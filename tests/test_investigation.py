"""调查循环：结论必须有证据支撑，查询失败不得变成证据。"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest

from aiops.actions.executor import AutoExecutionError
from aiops.config import Settings
from aiops.contracts import (
    ActionKind,
    ActionOrigin,
    ActionProposal,
    ActionTarget,
    Budget,
    Incident,
    Investigation,
    InvestigationStatus,
    RiskDecision,
    RiskDisposition,
    TimeRange,
    ToolError,
    ToolResult,
)
from aiops.db import Store
from aiops.investigation.runtime import InvestigationRuntime
from aiops.investigation.schemas import (
    Assessment,
    HypothesisDraft,
    HypothesisUpdate,
    PlanRequest,
    QueryRequest,
    RunbookUse,
    Verdict,
)
from aiops.model import ModelCall

DSN = Settings().database_url
pytestmark = pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL，跳过真实库测试")

START = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)
WINDOW = TimeRange(start=START, end=START + timedelta(minutes=15))


class ScriptedModel:
    def __init__(self, responses):
        self._responses = list(responses)
        self.messages = []

    async def complete_json(
        self, messages, response_model, *, schema_name="response", temperature=None
    ):
        self.messages.append(messages)
        value = self._responses.pop(0)
        assert isinstance(value, response_model)
        return ModelCall(value=value, model="scripted", attempts=1, usage={})


class ScriptedToolbox:
    def __init__(self, results):
        self._results = list(results)
        self.requests = []

    async def run(self, context, time_range, request):
        self.requests.append(request)
        return self._results.pop(0)


def query(hypothesis=0, tool="prometheus.query_range"):
    return QueryRequest(
        hypothesis=hypothesis,
        tool=tool,
        purpose="确认探测是否失败",
        expression='probe_success{job="frontend-probe"}',
        lookback_seconds=120,
        step_seconds=15,
        selector="",
        involved_name="",
    )


def success_result():
    return ToolResult(
        tool_call_id=f"call-{uuid4().hex[:8]}",
        source="prometheus",
        query={"expression": "probe_success"},
        time_range=WINDOW,
        collected_at=START,
        data={"series": [{"labels": {}, "points": [[1.0, 0.0]]}]},
        raw_ref="evidence/synthetic/probe.json",
    )


def failure_result():
    return ToolResult(
        tool_call_id=f"call-{uuid4().hex[:8]}",
        source="prometheus",
        query={"expression": "probe_success"},
        time_range=WINDOW,
        collected_at=START,
        error=ToolError(code="timeout", message="查询超时", retryable=True),
    )


@pytest.fixture
def ids():
    tag = uuid4().hex[:12]
    values = {
        "incident": f"inc-{tag}",
        "investigation": f"inv-{tag}",
        "correlation": f"corr-{tag}",
    }
    yield values
    with psycopg.connect(DSN) as conn:
        conn.execute("DELETE FROM audit_events WHERE correlation_id = %s", (values["correlation"],))
        conn.execute(
            "DELETE FROM audit_events WHERE correlation_id = %s", (values["investigation"],)
        )
        conn.execute("DELETE FROM evidence WHERE investigation_id = %s", (values["investigation"],))
        conn.execute(
            "DELETE FROM tool_calls WHERE investigation_id = %s", (values["investigation"],)
        )
        conn.execute(
            "DELETE FROM hypotheses WHERE investigation_id = %s", (values["investigation"],)
        )
        conn.execute("DELETE FROM investigations WHERE id = %s", (values["investigation"],))
        conn.execute("DELETE FROM incidents WHERE id = %s", (values["incident"],))
        conn.execute("DELETE FROM checkpoints WHERE thread_id = %s", (values["investigation"],))
        conn.execute(
            "DELETE FROM checkpoint_writes WHERE thread_id = %s", (values["investigation"],)
        )


def build(ids, *, budget_model_calls=4, budget_tool_calls=4):
    incident = Incident(
        id=ids["incident"],
        environment="local",
        cluster="vigil-lab",
        namespace="vigil-shop",
        service="frontend",
        trigger="manual",
        symptoms="首页返回 500",
        time_range=WINDOW,
    )
    investigation = Investigation(
        id=ids["investigation"],
        incident_id=incident.id,
        status=InvestigationStatus.RUNNING,
        model="scripted",
        policy_version="test",
        budget=Budget(
            max_tool_calls=budget_tool_calls,
            max_model_calls=budget_model_calls,
            max_wall_clock_seconds=120,
        ),
        started_at=START,
    )
    return incident, investigation


class FakeProposer:
    def __init__(self, proposal_id="act-model-1"):
        self.proposal_id = proposal_id
        self.calls = []

    async def propose_restart_pod(
        self,
        *,
        pod,
        service,
        reason,
        actor,
        path="/",
        namespace=None,
        investigation_id=None,
        origin=None,
        evidence_ids=None,
    ):
        self.calls.append(
            {
                "kind": "restart_pod",
                "pod": pod,
                "service": service,
                "path": path,
                "reason": reason,
                "actor": actor,
                "namespace": namespace,
                "investigation_id": investigation_id,
                "origin": origin,
                "evidence_ids": evidence_ids or [],
            }
        )
        return (
            ActionProposal(
                id=self.proposal_id,
                kind=ActionKind.RESTART_POD,
                target=ActionTarget(
                    cluster="vigil-lab",
                    namespace="vigil-shop",
                    kind="Pod",
                    name=pod,
                    uid="pod-uid-1",
                    resource_version="1",
                ),
                parameters={"pod": pod, "service": service},
                idempotency_key="restart:pod-uid-1",
                reason=reason,
                created_at=START,
                origin=ActionOrigin.MODEL,
                evidence_ids=evidence_ids or [],
            ),
            RiskDecision(
                proposal_id=self.proposal_id,
                disposition=RiskDisposition.APPROVAL,
                policy_version="test",
                reasons=["test"],
                requires_compensation=False,
            ),
        )

    async def propose_rollback(
        self,
        *,
        deployment,
        reason,
        actor,
        namespace=None,
        investigation_id=None,
        origin=None,
        evidence_ids=None,
    ):
        self.calls.append(
            {
                "deployment": deployment,
                "reason": reason,
                "actor": actor,
                "namespace": namespace,
                "investigation_id": investigation_id,
                "origin": origin,
                "evidence_ids": evidence_ids or [],
            }
        )
        return (
            ActionProposal(
                id=self.proposal_id,
                kind=ActionKind.ROLLOUT_UNDO,
                target=ActionTarget(
                    cluster="vigil-lab",
                    namespace="vigil-shop",
                    kind="Deployment",
                    name=deployment,
                    uid="uid-1",
                    resource_version="1",
                ),
                parameters={},
                idempotency_key="key-1",
                reason=reason,
                created_at=START,
                origin=origin,
                investigation_id=investigation_id,
                evidence_ids=evidence_ids or [],
            ),
            RiskDecision(
                proposal_id=self.proposal_id,
                disposition=RiskDisposition.APPROVAL,
                policy_version="test",
                reasons=["需要审批"],
                requires_compensation=False,
            ),
        )


async def run_case(
    ids,
    model,
    toolbox,
    *,
    budget_model_calls=4,
    budget_tool_calls=4,
    tmp_path=None,
    proposer=None,
):
    incident, investigation = build(
        ids, budget_model_calls=budget_model_calls, budget_tool_calls=budget_tool_calls
    )
    async with Store.open(DSN) as store:
        await store.migrate()
        await store.save_incident(incident)
        await store.create_investigation(investigation)
        runtime = InvestigationRuntime(
            store=store,
            toolbox=toolbox,
            model=model,
            dsn=DSN,
            incident=incident,
            investigation=investigation,
            observation_window=WINDOW,
            runtime_path=tmp_path,
            proposer=proposer,
        )
        report = await runtime.run()
        evidence = await store.list_evidence(investigation.id)
        calls = await store.list_tool_calls(investigation.id)
        saved = await store.get_investigation(investigation.id)
    return report, evidence, calls, saved


def test_supported_hypothesis_becomes_root_cause(ids, tmp_path):
    model = ScriptedModel(
        [
            PlanRequest(
                hypotheses=[
                    HypothesisDraft(
                        statement="坏发布导致首页失败",
                        expected_observations=["probe_success 为 0"],
                        rationale="首页报错且新版本刚发布",
                    )
                ],
                queries=[query()],
            ),
            Assessment(
                hypotheses=[HypothesisUpdate(index=0, status="supported", rationale="探测为 0")],
                verdicts=[Verdict(evidence_index=0, relation="supporting")],
                next_queries=[],
                finish=True,
                root_cause=True,
                conclusion="新版本导致 productcatalogservice 不可用，首页返回 500",
                action_kind="rollout_undo",
                action_deployment="productcatalogservice",
                action_pod="",
                action_service="",
                action_path="",
                action_reason="回退到上一版本",
                action_evidence=[0],
                runbook_uses=[],
            ),
        ]
    )
    report, evidence, calls, saved = asyncio.run(
        run_case(ids, model, ScriptedToolbox([success_result()]), tmp_path=tmp_path)
    )

    assert report.status is InvestigationStatus.COMPLETED
    assert saved.status is InvestigationStatus.COMPLETED
    assert "productcatalogservice" in (saved.conclusion or "")
    assert [item.relation for item in evidence] == ["supporting"]
    assert len(calls) == 1 and calls[0].error_code is None
    assert report.hypotheses[0]["status"] == "supported"


def test_failed_query_is_not_turned_into_evidence(ids, tmp_path):
    model = ScriptedModel(
        [
            PlanRequest(
                hypotheses=[
                    HypothesisDraft(
                        statement="坏发布导致首页失败",
                        expected_observations=["probe_success 为 0"],
                        rationale="首页报错",
                    )
                ],
                queries=[query()],
            ),
            Assessment(
                hypotheses=[HypothesisUpdate(index=0, status="inconclusive", rationale="查询失败")],
                verdicts=[],
                next_queries=[],
                finish=True,
                root_cause=False,
                conclusion="取证失败，无法判断",
                action_kind="none",
                action_deployment="",
                action_pod="",
                action_service="",
                action_path="",
                action_reason="",
                action_evidence=[],
                runbook_uses=[],
            ),
        ]
    )
    report, evidence, calls, saved = asyncio.run(
        run_case(ids, model, ScriptedToolbox([failure_result()]), tmp_path=tmp_path)
    )

    assert saved.status is InvestigationStatus.INCONCLUSIVE
    assert evidence == []
    assert len(calls) == 1 and calls[0].error_code == "timeout"
    assert saved.conclusion is None
    assert "证据不足" in (saved.termination_reason or "")


def test_model_budget_exhaustion_aborts_instead_of_guessing(ids, tmp_path):
    model = ScriptedModel(
        [
            PlanRequest(
                hypotheses=[
                    HypothesisDraft(
                        statement="坏发布导致首页失败",
                        expected_observations=["probe_success 为 0"],
                        rationale="首页报错",
                    )
                ],
                queries=[],
            )
        ]
    )
    report, evidence, calls, saved = asyncio.run(
        run_case(
            ids,
            model,
            ScriptedToolbox([]),
            budget_model_calls=1,
            tmp_path=tmp_path,
        )
    )

    assert saved.status is InvestigationStatus.ABORTED
    assert "model_calls" in (saved.termination_reason or "")
    assert evidence == [] and calls == []


def test_checkpoint_is_persisted_for_the_thread(ids, tmp_path):
    model = ScriptedModel(
        [
            PlanRequest(
                hypotheses=[
                    HypothesisDraft(
                        statement="坏代理导致探测失败",
                        expected_observations=["probe_success 为 0"],
                        rationale="探测失败",
                    )
                ],
                queries=[],
            ),
            Assessment(
                hypotheses=[],
                verdicts=[],
                next_queries=[],
                finish=True,
                root_cause=False,
                conclusion="没有可用证据",
                action_kind="none",
                action_deployment="",
                action_pod="",
                action_service="",
                action_path="",
                action_reason="",
                action_evidence=[],
                runbook_uses=[],
            ),
        ]
    )
    asyncio.run(run_case(ids, model, ScriptedToolbox([]), tmp_path=tmp_path))

    with psycopg.connect(DSN) as conn:
        count = conn.execute(
            "SELECT count(*) FROM checkpoints WHERE thread_id = %s", (ids["investigation"],)
        ).fetchone()[0]
    assert count > 0


def test_model_proposal_is_created_with_supporting_evidence(ids, tmp_path):
    proposer = FakeProposer()
    model = ScriptedModel(
        [
            PlanRequest(
                hypotheses=[
                    HypothesisDraft(
                        statement="坏发布导致首页失败",
                        expected_observations=["probe_success 为 0"],
                        rationale="首页报错",
                    )
                ],
                queries=[query()],
            ),
            Assessment(
                hypotheses=[HypothesisUpdate(index=0, status="supported", rationale="探测为 0")],
                verdicts=[Verdict(evidence_index=0, relation="supporting")],
                next_queries=[],
                finish=True,
                root_cause=True,
                conclusion="productcatalogservice 新版本导致首页 500",
                action_kind="rollout_undo",
                action_deployment="productcatalogservice",
                action_pod="",
                action_service="",
                action_path="",
                action_reason="回退到上一版本",
                action_evidence=[0],
                runbook_uses=[],
            ),
        ]
    )
    report, evidence, _, _ = asyncio.run(
        run_case(
            ids,
            model,
            ScriptedToolbox([success_result()]),
            tmp_path=tmp_path,
            proposer=proposer,
        )
    )

    assert report.proposal_id == "act-model-1"
    assert len(proposer.calls) == 1
    call = proposer.calls[0]
    assert call["origin"] is ActionOrigin.MODEL
    assert call["evidence_ids"] == [evidence[0].id]
    assert call["investigation_id"] == ids["investigation"]
    assert call["deployment"] == "productcatalogservice"


def test_restart_pod_assessment_proposes_pod_restart(ids, tmp_path):
    proposer = FakeProposer()
    model = ScriptedModel(
        [
            PlanRequest(
                hypotheses=[
                    HypothesisDraft(
                        statement="Pod 级状态损坏导致业务请求失败",
                        expected_observations=["Pod 内请求全部失败"],
                        rationale="应用日志持续 500",
                    )
                ],
                queries=[query()],
            ),
            Assessment(
                hypotheses=[HypothesisUpdate(index=0, status="supported", rationale="失败持续")],
                verdicts=[Verdict(evidence_index=0, relation="supporting")],
                next_queries=[],
                finish=True,
                root_cause=True,
                conclusion="重建该 Pod 可以清除状态",
                action_kind="restart_pod",
                action_deployment="",
                action_pod="rebuild-demo-abc",
                action_service="rebuild-demo",
                action_path="/work",
                action_reason="Pod 级状态损坏",
                action_evidence=[0],
                runbook_uses=[],
            ),
        ]
    )
    report, evidence, _, _ = asyncio.run(
        run_case(
            ids,
            model,
            ScriptedToolbox([success_result()]),
            tmp_path=tmp_path,
            proposer=proposer,
        )
    )

    assert report.proposal_id == "act-model-1"
    call = proposer.calls[0]
    assert call["kind"] == "restart_pod"
    assert call["pod"] == "rebuild-demo-abc"
    assert call["service"] == "rebuild-demo"
    assert call["path"] == "/work"
    assert call["evidence_ids"] == [evidence[0].id]


def _restart_assessment(**overrides) -> Assessment:
    values = {
        "hypotheses": [HypothesisUpdate(index=0, status="supported", rationale="失败持续")],
        "verdicts": [Verdict(evidence_index=0, relation="supporting")],
        "next_queries": [],
        "finish": True,
        "root_cause": True,
        "conclusion": "重建该 Pod 可以清除状态",
        "action_kind": "restart_pod",
        "action_deployment": "",
        "action_pod": "rebuild-demo-abc",
        "action_service": "rebuild-demo",
        "action_path": "/work",
        "action_reason": "Pod 级状态损坏",
        "action_evidence": [0],
        "runbook_uses": [],
    }
    values.update(overrides)
    return Assessment(**values)


def _run_restart_case(ids, tmp_path, proposer, assessment):
    model = ScriptedModel(
        [
            PlanRequest(
                hypotheses=[
                    HypothesisDraft(
                        statement="Pod 级状态损坏",
                        expected_observations=["请求失败"],
                        rationale="日志",
                    )
                ],
                queries=[query()],
            ),
            assessment,
        ]
    )
    return asyncio.run(
        run_case(
            ids, model, ScriptedToolbox([success_result()]), tmp_path=tmp_path, proposer=proposer
        )
    )


def test_auto_execution_failure_keeps_the_proposal_id(ids, tmp_path):
    """提案已落库但执行没完成时，报告不能把它当成“未提案”。"""

    class BusyProposer(FakeProposer):
        async def propose_restart_pod(self, **kwargs):
            raise AutoExecutionError("target_busy", "目标已有未完成的执行", "act-persisted-auto")

    report, _, _, _ = _run_restart_case(ids, tmp_path, BusyProposer(), _restart_assessment())

    assert report.proposal_id == "act-persisted-auto"


def test_unknown_runbook_aborts_before_any_proposal(ids, tmp_path):
    """结论先被 runbook 校验拒绝时，不能在 abort 前把动作提出去（自动允许会立即执行）。"""
    proposer = FakeProposer()

    report, _, _, _ = _run_restart_case(
        ids,
        tmp_path,
        proposer,
        _restart_assessment(
            runbook_uses=[RunbookUse(runbook_id="rb-ghost", accepted=True, reason="猜的")]
        ),
    )

    assert report.status.value == "aborted"
    assert proposer.calls == []


def test_restart_without_root_cause_is_not_proposed(ids, tmp_path):
    proposer = FakeProposer()

    report, _, _, _ = _run_restart_case(
        ids, tmp_path, proposer, _restart_assessment(root_cause=False, conclusion="")
    )

    assert report.proposal_id is None
    assert proposer.calls == []


def test_restart_with_empty_conclusion_is_not_proposed(ids, tmp_path):
    """空结论最终会被判为 inconclusive，不能在这种状态下先执行动作。"""
    proposer = FakeProposer()

    report, _, _, _ = _run_restart_case(ids, tmp_path, proposer, _restart_assessment(conclusion=""))

    assert proposer.calls == []
    assert report.proposal_id is None


def test_restart_citing_refuting_evidence_is_not_proposed(ids, tmp_path):
    proposer = FakeProposer()
    assessment = _restart_assessment(
        verdicts=[Verdict(evidence_index=0, relation="refuting")],
        hypotheses=[HypothesisUpdate(index=0, status="refuted", rationale="请求正常")],
    )

    report, _, _, _ = _run_restart_case(ids, tmp_path, proposer, assessment)

    assert report.proposal_id is None
    assert proposer.calls == []


def test_restart_without_service_is_not_proposed(ids, tmp_path):
    proposer = FakeProposer()

    report, _, _, _ = _run_restart_case(
        ids, tmp_path, proposer, _restart_assessment(action_service="")
    )

    assert report.proposal_id is None
    assert proposer.calls == []


def test_action_without_supporting_evidence_is_not_proposed(ids, tmp_path):
    proposer = FakeProposer()
    model = ScriptedModel(
        [
            PlanRequest(
                hypotheses=[
                    HypothesisDraft(
                        statement="坏发布导致首页失败",
                        expected_observations=["probe_success 为 0"],
                        rationale="首页报错",
                    )
                ],
                queries=[query()],
            ),
            Assessment(
                hypotheses=[HypothesisUpdate(index=0, status="refuted", rationale="探测正常")],
                verdicts=[Verdict(evidence_index=0, relation="refuting")],
                next_queries=[],
                finish=True,
                root_cause=False,
                conclusion="证据不支持",
                action_kind="rollout_undo",
                action_deployment="productcatalogservice",
                action_pod="",
                action_service="",
                action_path="",
                action_reason="不该被执行",
                action_evidence=[],
                runbook_uses=[],
            ),
        ]
    )
    report, _, _, _ = asyncio.run(
        run_case(
            ids,
            model,
            ScriptedToolbox([success_result()]),
            tmp_path=tmp_path,
            proposer=proposer,
        )
    )

    assert report.proposal_id is None
    assert proposer.calls == []


def knowledge_result():
    return ToolResult(
        tool_call_id=f"call-{uuid4().hex[:8]}",
        source="knowledge",
        query={"tool": "knowledge.list_runbooks"},
        time_range=WINDOW,
        collected_at=START,
        data={"items": [{"id": "rb-oom-limit", "version": 1, "title": "x", "summary": "y"}]},
    )


def test_runbook_use_is_recorded(ids, tmp_path):
    proposer = FakeProposer()
    model = ScriptedModel(
        [
            PlanRequest(
                hypotheses=[
                    HypothesisDraft(
                        statement="依赖解析失败",
                        expected_observations=["日志出现解析错误"],
                        rationale="报错文本",
                    )
                ],
                queries=[query(tool="knowledge.list_runbooks")],
            ),
            Assessment(
                hypotheses=[HypothesisUpdate(index=0, status="supported", rationale="证据")],
                verdicts=[Verdict(evidence_index=0, relation="supporting")],
                next_queries=[],
                finish=True,
                root_cause=True,
                conclusion="按 runbook rb-oom-limit 的顺序核对",
                action_kind="none",
                action_deployment="",
                action_pod="",
                action_service="",
                action_path="",
                action_reason="",
                action_evidence=[],
                runbook_uses=[
                    RunbookUse(runbook_id="rb-oom-limit", accepted=True, reason="与证据相符")
                ],
            ),
        ]
    )
    report, _, _, _ = asyncio.run(
        run_case(
            ids, model, ScriptedToolbox([knowledge_result()]), tmp_path=tmp_path, proposer=proposer
        )
    )

    assert [use["runbook_id"] for use in report.runbook_uses] == ["rb-oom-limit"]
    assert report.runbook_uses[0]["accepted"] is True
    with psycopg.connect(DSN) as conn:
        events = [
            row[0]
            for row in conn.execute(
                "SELECT event FROM audit_events WHERE correlation_id = %s", (ids["investigation"],)
            )
        ]
    assert "runbook.accepted" in events


def _runbook_assessment(**overrides) -> Assessment:
    values = {
        "hypotheses": [HypothesisUpdate(index=0, status="supported", rationale="证据")],
        "verdicts": [Verdict(evidence_index=0, relation="supporting")],
        "next_queries": [],
        "finish": True,
        "root_cause": True,
        "conclusion": "按条目核对",
        "action_kind": "none",
        "action_deployment": "",
        "action_pod": "",
        "action_service": "",
        "action_path": "",
        "action_reason": "",
        "action_evidence": [],
        "runbook_uses": [],
    }
    values.update(overrides)
    return Assessment(**values)


def _run_knowledge_case(ids, tmp_path, assessment):
    model = ScriptedModel(
        [
            PlanRequest(
                hypotheses=[
                    HypothesisDraft(statement="x", expected_observations=["y"], rationale="z")
                ],
                queries=[query(tool="knowledge.list_runbooks")],
            ),
            assessment,
        ]
    )
    return asyncio.run(
        run_case(ids, model, ScriptedToolbox([knowledge_result()]), tmp_path=tmp_path)
    )


def test_unknown_runbook_citation_aborts_the_investigation(ids, tmp_path):
    report, _, _, _ = _run_knowledge_case(
        ids,
        tmp_path,
        _runbook_assessment(
            runbook_uses=[RunbookUse(runbook_id="rb-ghost", accepted=True, reason="猜的")]
        ),
    )

    assert report.status.value == "aborted"
    assert "rb-ghost" in (report.termination_reason or "")


def test_unjudged_runbook_candidate_aborts_the_investigation(ids, tmp_path):
    report, _, _, _ = _run_knowledge_case(ids, tmp_path, _runbook_assessment(runbook_uses=[]))

    assert report.status.value == "aborted"
    assert "rb-oom-limit" in (report.termination_reason or "")


def test_knowledge_only_conclusion_is_not_accepted(ids, tmp_path):
    model = ScriptedModel(
        [
            PlanRequest(
                hypotheses=[
                    HypothesisDraft(
                        statement="内存不足", expected_observations=["集群证据"], rationale="x"
                    )
                ],
                queries=[query()],
            ),
            _runbook_assessment(conclusion="根据 rb-oom-limit 已确认内存不足"),
        ]
    )
    report, _, _, _ = asyncio.run(
        run_case(ids, model, ScriptedToolbox([knowledge_result()]), tmp_path=tmp_path)
    )

    assert report.status.value == "inconclusive"
    assert report.conclusion is None


def test_knowledge_evidence_cannot_support_an_action(ids, tmp_path):
    proposer = FakeProposer()
    model = ScriptedModel(
        [
            PlanRequest(
                hypotheses=[
                    HypothesisDraft(statement="x", expected_observations=["y"], rationale="z")
                ],
                queries=[query()],
            ),
            _runbook_assessment(
                action_kind="rollout_undo",
                action_deployment="productcatalogservice",
                action_evidence=[0],
            ),
        ]
    )
    report, _, _, _ = asyncio.run(
        run_case(
            ids,
            model,
            ScriptedToolbox([knowledge_result()]),
            tmp_path=tmp_path,
            proposer=proposer,
        )
    )

    assert report.proposal_id is None
    assert proposer.calls == []
