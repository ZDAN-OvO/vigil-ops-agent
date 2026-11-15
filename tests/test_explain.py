"""只读追问：只用已存记录回答，引用越界拒绝，除审计外不写任何东西。"""

import asyncio
from types import SimpleNamespace

import pytest

from aiops.investigation.explain import ExplainError, ExplainService, Explanation


class FakeStore:
    def __init__(self, *, evidence=None, investigation=True, status="completed", proposals=None):
        self.calls = []
        self.audit = []
        self._proposals = proposals or []
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

    async def list_proposals_for_investigation(self, investigation_id):
        self.calls.append("list_proposals_for_investigation")
        return self._proposals

    async def get_approval(self, proposal_id):
        self.calls.append("get_approval")
        return None

    async def append_audit(self, events):
        self.calls.append("append_audit")
        self.audit.extend(events)


class FakeModel:
    def __init__(self, explanation):
        self._explanation = explanation
        self.calls = 0
        self.payload = None

    async def complete_json(
        self, messages, response_model, *, schema_name="response", temperature=None
    ):
        self.calls += 1
        import json

        self.payload = json.loads(messages[-1]["content"])
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
        "list_proposals_for_investigation",
        "get_approval",
        "append_audit",
    }


def test_explain_audits_record_the_same_state_snapshot():
    store = FakeStore()
    model = FakeModel(explanation())

    asyncio.run(ExplainService(store=store, model=model).answer("inv-1", "为什么？"))

    requested, answered = store.audit
    assert requested.payload["snapshot"] == answered.payload["snapshot"]
    snapshot = requested.payload["snapshot"]
    assert snapshot["version"] == 2
    assert [item["id"] for item in snapshot["evidence"]] == ["ev-1"]
    assert len(snapshot["conclusion_hash"]) == 64


class MutatingModel(FakeModel):
    def __init__(self, explanation, store):
        super().__init__(explanation)
        self._store = store

    async def complete_json(self, *args, **kwargs):
        result = await super().complete_json(*args, **kwargs)
        self._store._evidence.append(evidence_row("ev-2"))
        return result


def test_explain_records_post_call_state_when_something_changes():
    store = FakeStore()
    model = MutatingModel(explanation(), store)

    asyncio.run(ExplainService(store=store, model=model).answer("inv-1", "为什么？"))

    requested, answered = store.audit
    assert requested.payload["snapshot"]["evidence"] != answered.payload["snapshot"]["evidence"]


def test_rejection_also_records_the_post_call_snapshot():
    store = FakeStore()
    model = FakeModel(explanation(citations=["ev-404"]))

    with pytest.raises(ExplainError):
        asyncio.run(ExplainService(store=store, model=model).answer("inv-1", "为什么？"))

    requested, rejected = store.audit
    assert rejected.event == "explain.rejected"
    assert rejected.payload["request_id"] == requested.payload["request_id"]
    assert rejected.payload["snapshot"] == requested.payload["snapshot"]


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


class FakeAgentStore(FakeStore):
    """单 Agent 调查：没有旧 evidence 行，只有观测账本与关系。"""

    def __init__(self, observations=None, relations=None, evidence=None):
        super().__init__(evidence=evidence if evidence is not None else [])
        self._observations = observations if observations is not None else [observation("obs-1")]
        self._relations = relations if relations is not None else [relation("obs-1", "hyp-1")]

    async def get_task(self, investigation_id):
        self.calls.append("get_task")
        return SimpleNamespace(runtime_version="langchain-agent-1", phase="terminal")

    async def list_observations(self, investigation_id):
        self.calls.append("list_observations")
        return self._observations

    async def list_hypothesis_evidence(self, investigation_id):
        self.calls.append("list_hypothesis_evidence")
        return self._relations


def observation(observation_id, status="ok", summary="currencyservice OOMKilled"):
    return SimpleNamespace(
        id=observation_id,
        source="kubernetes",
        status=SimpleNamespace(value=status),
        summary=summary,
        observation=None,
        raw_ref=f"evidence/{observation_id}.json",
        truncated=False,
    )


def relation(observation_id, hypothesis_id, relation_text="supporting"):
    return SimpleNamespace(
        id=f"he-{observation_id}",
        observation_id=observation_id,
        hypothesis_id=hypothesis_id,
        relation=relation_text,
        rationale="重启次数与内存上限一致",
    )


def test_agent_ledger_answer_cites_observations_and_snapshots_relations():
    store = FakeAgentStore()
    model = FakeModel(explanation(citations=["obs-1"]))

    result = asyncio.run(ExplainService(store=store, model=model).answer("inv-1", "为什么？"))

    assert result.citations == ["obs-1"]
    requested, answered = store.audit
    assert requested.payload["snapshot"] == answered.payload["snapshot"]
    snapshot = requested.payload["snapshot"]
    assert snapshot["version"] == 3
    assert [item["id"] for item in snapshot["observations"]] == ["obs-1"]
    assert [item["id"] for item in snapshot["relations"]] == ["he-obs-1"]
    assert [item["id"] for item in model.payload["records"]] == ["obs-1"]


def test_agent_ledger_records_carry_relation_and_exclude_non_ok_observations():
    store = FakeAgentStore(
        observations=[observation("obs-1"), observation("obs-2", status="error")]
    )
    model = FakeModel(explanation(citations=["obs-1"]))

    asyncio.run(ExplainService(store=store, model=model).answer("inv-1", "为什么？"))

    records = model.payload["records"]
    assert [item["id"] for item in records] == ["obs-1"]
    assert records[0]["relation"] == "supporting"
    assert records[0]["rationale"] == "重启次数与内存上限一致"


def test_agent_ledger_rejects_citing_a_failed_observation():
    store = FakeAgentStore(observations=[observation("obs-err", status="error")], relations=[])
    model = FakeModel(explanation(citations=["obs-err"]))

    with pytest.raises(ExplainError) as caught:
        asyncio.run(ExplainService(store=store, model=model).answer("inv-1", "为什么？"))

    assert caught.value.code == "no_evidence"
    assert model.calls == 0
