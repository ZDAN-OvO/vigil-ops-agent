"""调查循环：结论必须有证据支撑，查询失败不得变成证据。"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest

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
                action_reason="回退到上一版本",
                action_evidence=[0],
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
                action_reason="",
                action_evidence=[],
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
                action_reason="",
                action_evidence=[],
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
                action_reason="回退到上一版本",
                action_evidence=[0],
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
                action_reason="不该被执行",
                action_evidence=[],
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
