"""调查测试共用的脚本模型、工具箱与一次调查的快捷入口。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from db_env import DSN

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
    QueryRequest,
)
from aiops.model import ModelCall

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


def knowledge_result():
    return ToolResult(
        tool_call_id=f"call-{uuid4().hex[:8]}",
        source="knowledge",
        query={"tool": "knowledge.list_runbooks"},
        time_range=WINDOW,
        collected_at=START,
        data={"items": [{"id": "rb-oom-limit", "version": 1, "title": "x", "summary": "y"}]},
    )
