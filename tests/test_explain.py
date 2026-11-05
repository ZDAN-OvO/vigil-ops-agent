"""只读追问：只用已存记录回答，引用越界拒绝，除审计外不写任何东西。"""

import asyncio
from types import SimpleNamespace

import pytest

from aiops.investigation.explain import ExplainError, ExplainService, Explanation


class FakeStore:
    def __init__(self, *, evidence=None, investigation=True, status="completed"):
        self.calls = []
        self.audit = []
        self._evidence = evidence if evidence is not None else [evidence_row("ev-1")]
        self._investigation = (
            SimpleNamespace(
                id="inv-1",
                incident_id="inc-1",
                status=SimpleNamespace(value=status),
                conclusion="内存限额不足",
                termination_reason=None,
            )
            if investigation
            else None
        )

    async def get_investigation(self, investigation_id):
        self.calls.append("get_investigation")
        return self._investigation

    async def get_incident(self, incident_id):
        self.calls.append("get_incident")
        return SimpleNamespace(service="frontend", symptoms="首页 500", context=[])

    async def list_hypotheses(self, investigation_id):
        self.calls.append("list_hypotheses")
        return []

    async def list_evidence(self, investigation_id):
        self.calls.append("list_evidence")
        return self._evidence

    async def append_audit(self, events):
        self.calls.append("append_audit")
        self.audit.extend(events)


class FakeModel:
    def __init__(self, explanation):
        self._explanation = explanation
        self.calls = 0

    async def complete_json(
        self, messages, response_model, *, schema_name="response", temperature=None
    ):
        self.calls += 1
        return SimpleNamespace(value=self._explanation, model="fake-model")


def evidence_row(evidence_id):
    return SimpleNamespace(
        id=evidence_id, source="kubernetes", relation="supporting", observation="x"
    )


def explanation(**overrides):
    values = {
        "answer": "因为 currencyservice 反复 OOMKilled。",
        "citations": ["ev-1"],
        "missing": [],
        "needs_new_query": False,
    }
    values.update(overrides)
    return Explanation(**values)


def test_explain_answers_with_existing_evidence_only():
    store = FakeStore()
    model = FakeModel(explanation())

    result = asyncio.run(ExplainService(store=store, model=model).answer("inv-1", "为什么？"))

    assert result.answer.startswith("因为")
    assert result.citations == ["ev-1"]
    assert model.calls == 1
    events = [event.event for event in store.audit]
    assert events == ["explain.requested", "explain.answered"]
    # 只读：除审计外没有写操作，也没有新的工具调用
    assert set(store.calls) <= {
        "get_investigation",
        "get_incident",
        "list_hypotheses",
        "list_evidence",
        "append_audit",
    }


def test_unknown_citation_is_rejected():
    store = FakeStore()
    model = FakeModel(explanation(citations=["ev-404"]))

    with pytest.raises(ExplainError) as caught:
        asyncio.run(ExplainService(store=store, model=model).answer("inv-1", "为什么？"))

    assert caught.value.code == "unknown_citation"
    assert [event.event for event in store.audit] == ["explain.requested", "explain.rejected"]


def test_missing_information_is_reported_not_invented():
    store = FakeStore()
    model = FakeModel(
        explanation(
            answer="记录里没有发布记录。",
            citations=[],
            missing=["缺少 Deployment 的 change-cause 记录"],
            needs_new_query=True,
        )
    )

    result = asyncio.run(
        ExplainService(store=store, model=model).answer("inv-1", "上次发布是谁做的？")
    )

    assert result.needs_new_query is True
    assert result.missing == ["缺少 Deployment 的 change-cause 记录"]
    assert result.citations == []


def test_investigation_without_evidence_cannot_be_explained():
    store = FakeStore(evidence=[])
    model = FakeModel(explanation())

    with pytest.raises(ExplainError) as caught:
        asyncio.run(ExplainService(store=store, model=model).answer("inv-1", "为什么？"))

    assert caught.value.code == "no_evidence"
    assert model.calls == 0


def test_unknown_investigation_is_rejected():
    store = FakeStore(investigation=False)
    model = FakeModel(explanation())

    with pytest.raises(ExplainError) as caught:
        asyncio.run(ExplainService(store=store, model=model).answer("inv-missing", "为什么？"))

    assert caught.value.code == "not_found"


def test_cited_answer_may_carry_caveats():
    store = FakeStore()
    model = FakeModel(
        explanation(
            answer="因为反复 OOMKilled。",
            citations=["ev-1"],
            missing=["缺少节点磁盘指标"],
            needs_new_query=False,
        )
    )

    result = asyncio.run(ExplainService(store=store, model=model).answer("inv-1", "为什么？"))

    assert result.citations == ["ev-1"]
    assert result.missing == ["缺少节点磁盘指标"]


def test_missing_without_new_query_is_rejected():
    store = FakeStore()
    model = FakeModel(
        explanation(
            answer="没有审批记录。", citations=[], missing=["审批记录"], needs_new_query=False
        )
    )

    with pytest.raises(ExplainError) as caught:
        asyncio.run(ExplainService(store=store, model=model).answer("inv-1", "谁批准的？"))

    assert caught.value.code == "inconsistent_answer"
    assert [event.event for event in store.audit] == ["explain.requested", "explain.rejected"]


def test_uncited_answer_without_missing_is_rejected():
    store = FakeStore()
    model = FakeModel(explanation(answer="上次发布由张三批准。", citations=[], missing=[]))

    with pytest.raises(ExplainError) as caught:
        asyncio.run(ExplainService(store=store, model=model).answer("inv-1", "谁批准的？"))

    assert caught.value.code == "unsupported_answer"


@pytest.mark.parametrize("status", ["running", "inconclusive", "aborted", "pending"])
def test_only_completed_investigation_is_explainable(status):
    store = FakeStore(status=status)
    model = FakeModel(explanation())

    with pytest.raises(ExplainError) as caught:
        asyncio.run(ExplainService(store=store, model=model).answer("inv-1", "为什么？"))

    assert caught.value.code == "not_completed"
    assert model.calls == 0
    assert store.audit == []
