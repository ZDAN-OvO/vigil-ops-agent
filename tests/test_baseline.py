"""规则基线：固定查询与规则表在零模型调用下给出可回查的结论。"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from aiops.contracts import (
    ActionKind,
    ActionOrigin,
    ActionProposal,
    ActionTarget,
    Budget,
    HypothesisStatus,
    Incident,
    Investigation,
    InvestigationStatus,
    RiskDecision,
    RiskDisposition,
    TimeRange,
    ToolError,
    ToolResult,
)
from aiops.investigation.baseline import RULES_VERSION, RulesBaseline

START = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)
WINDOW = TimeRange(start=START, end=START + timedelta(minutes=15))


class MemoryStore:
    def __init__(self, investigation):
        self.investigation = investigation
        self.hypotheses = []
        self.evidence = []
        self.calls = []
        self.audits = []
        self.usage = {"tool_calls": 0, "model_calls": 0}

    async def get_investigation(self, investigation_id):
        return self.investigation

    async def save_hypothesis(self, hypothesis):
        self.hypotheses.append(hypothesis)

    async def set_hypothesis_status(self, hypothesis_id, status):
        for item in self.hypotheses:
            if item.id == hypothesis_id:
                item.status = HypothesisStatus(status)

    async def save_tool_call(self, call):
        self.calls.append(call)

    async def save_evidence(self, evidence):
        self.evidence.append(evidence)

    async def set_evidence_relation(self, evidence_id, relation):
        for item in self.evidence:
            if item.id == evidence_id:
                item.relation = relation

    async def add_usage(self, investigation_id, *, tool_calls=0, model_calls=0):
        self.usage["tool_calls"] += tool_calls
        self.usage["model_calls"] += model_calls

    async def set_investigation_status(
        self,
        investigation_id,
        status,
        *,
        conclusion=None,
        termination_reason=None,
        finished_at=None,
    ):
        self.investigation = self.investigation.model_copy(
            update={
                "status": status,
                "conclusion": conclusion,
                "termination_reason": termination_reason,
                "finished_at": finished_at,
            }
        )

    async def append_audit(self, events):
        self.audits.extend(events)

    def events(self, name):
        return [item for item in self.audits if item.event == name]


class ScriptedToolbox:
    def __init__(self, results):
        self._results = list(results)
        self.requests = []

    async def run(self, context, time_range, request):
        self.requests.append(request)
        return self._results.pop(0)


class RecordingProposer:
    def __init__(self):
        self.calls = []

    async def propose_restart_pod(self, **kwargs):
        raise AssertionError("规则基线不提案重建 Pod")

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
                "origin": origin,
                "evidence_ids": evidence_ids or [],
                "namespace": namespace,
                "investigation_id": investigation_id,
            }
        )
        proposal_id = f"act-{uuid4().hex[:8]}"
        return (
            ActionProposal(
                id=proposal_id,
                kind=ActionKind.ROLLOUT_UNDO,
                target=ActionTarget(
                    cluster="vigil-lab",
                    namespace="vigil-shop",
                    kind="Deployment",
                    name=deployment,
                    uid="deploy-uid",
                    resource_version="1",
                ),
                parameters={"deployment": deployment},
                idempotency_key=f"key-{uuid4().hex[:8]}",
                reason=reason,
                created_at=START,
                origin=origin,
                investigation_id=investigation_id,
                evidence_ids=evidence_ids or [],
            ),
            RiskDecision(
                proposal_id=proposal_id,
                disposition=RiskDisposition.APPROVAL,
                policy_version="test",
                reasons=["test"],
                requires_compensation=False,
            ),
        )


def result(data, *, source="kubernetes", error=None):
    return ToolResult(
        tool_call_id=f"tc-{uuid4().hex[:8]}",
        source=source,
        query={},
        time_range=WINDOW,
        collected_at=START,
        data=None if error else data,
        error=error,
        raw_ref=None if error else "evidence/synthetic.json",
    )


def revision(number, *, image="repo/app:1", memory="128Mi", created=None):
    return {
        "revision": number,
        "image": image,
        "memory_limit": memory,
        "memory_request": memory,
        "replicas": 1,
        "ready_replicas": 1,
        "created_at": (created or START + timedelta(minutes=5)).isoformat(),
    }


def deployment(name, revisions, *, limits="128Mi", app=None):
    return {
        "name": name,
        "labels": {"app": app or name},
        "containers": [{"name": "server", "image": "repo/app:1", "limits": {"memory": limits}}],
        "revisions": revisions,
    }


def pod(name, app, *, oom=False, restarts=0, ready=True, finished=None, started=None):
    terminated = None
    if oom:
        terminated = {
            "reason": "OOMKilled",
            "exit_code": 137,
            "finished_at": (finished or START + timedelta(minutes=10)).isoformat(),
        }
    container = {
        "name": "server",
        "ready": ready,
        "restart_count": restarts,
        "reason": "CrashLoopBackOff" if oom else None,
        "last_terminated": terminated,
        "started_at": (started or START + timedelta(minutes=10)).isoformat(),
    }
    return {"name": name, "labels": {"app": app}, "containers": [container]}


def plan(deployments=None, pods=None, events=None, logs=None, probe=None, runbooks=None):
    return [
        result({"items": deployments or [], "count": len(deployments or [])}),
        result({"items": pods or [], "count": len(pods or [])}),
        result({"items": events or [], "count": len(events or [])}),
        result({"items": logs or [], "count": len(logs or [])}),
        result(
            {"series": [{"labels": {}, "points": [[0, probe]]}]}
            if probe is not None
            else {"series": []},
            source="prometheus",
        ),
        result({"items": runbooks or []}, source="knowledge"),
    ]


def build(*, budget_tool_calls=16, proposer=None):
    incident = Incident(
        id="inc-test",
        environment="local",
        cluster="vigil-lab",
        namespace="vigil-shop",
        service="frontend",
        trigger="manual",
        symptoms="首页 500",
        time_range=WINDOW,
    )
    investigation = Investigation(
        id="inv-test",
        incident_id=incident.id,
        status=InvestigationStatus.RUNNING,
        model=f"rules:{RULES_VERSION}",
        policy_version="mvp-1",
        budget=Budget(
            max_tool_calls=budget_tool_calls,
            max_model_calls=4,
            max_wall_clock_seconds=120,
        ),
        started_at=START,
    )
    return incident, investigation


def run_case(results, *, budget_tool_calls=16, proposer=None):
    incident, investigation = build(budget_tool_calls=budget_tool_calls, proposer=proposer)
    store = MemoryStore(investigation)
    toolbox = ScriptedToolbox(results)
    baseline = RulesBaseline(
        store=store,
        toolbox=toolbox,
        incident=incident,
        investigation=investigation,
        observation_window=WINDOW,
        runtime_path=None,
        proposer=proposer,
    )
    report = asyncio.run(baseline.run())
    return report, store, toolbox, investigation


def test_oom_killed_maps_to_memory_limit_and_rollback():
    proposer = RecordingProposer()
    results = plan(
        deployments=[
            deployment("currencyservice", [revision(1), revision(2, memory="16Mi")], limits="16Mi")
        ],
        pods=[pod("currencyservice-abc", "currencyservice", oom=True, restarts=4, ready=False)],
        probe=0,
    )

    report, store, toolbox, investigation = run_case(results, proposer=proposer)

    assert report.status is InvestigationStatus.COMPLETED
    assert "OOMKilled" in report.conclusion
    assert "16Mi" in report.conclusion and "128Mi" in report.conclusion
    assert [item.status for item in store.hypotheses] == [
        "supported",
        "inconclusive",
        "inconclusive",
    ]
    assert store.usage["model_calls"] == 0
    assert len(store.calls) == 6
    assert store.events("baseline.started")[0].payload["rules_version"] == RULES_VERSION
    assert store.events("baseline.rule")[0].payload["rule"] == "oom-limit"
    assert proposer.calls[0]["origin"] is ActionOrigin.RULES
    assert proposer.calls[0]["evidence_ids"]
    assert proposer.calls[0]["deployment"] == "currencyservice"
    assert report.proposal_id


def test_template_change_maps_to_recent_rollout():
    proposer = RecordingProposer()
    results = plan(
        deployments=[
            deployment(
                "productcatalogservice",
                [revision(20), revision(21, image="repo/app:canary")],
            )
        ],
        pods=[pod("productcatalogservice-abc", "productcatalogservice")],
        logs=[
            {
                "pod": "frontend-abc",
                "lines": [
                    '{"message":"failed to get product catalog: product catalog unavailable"}'
                ],
            }
        ],
        probe=1,
    )

    report, store, _, _ = run_case(results, proposer=proposer)

    assert report.status is InvestigationStatus.COMPLETED
    assert "观察窗内启动" in report.conclusion and "canary" in report.conclusion
    assert store.events("baseline.rule")[0].payload["rule"] == "recent-rollout"
    assert proposer.calls[0]["deployment"] == "productcatalogservice"


def test_new_workload_is_reported_without_a_rollback():
    proposer = RecordingProposer()
    results = plan(
        deployments=[deployment("redis-access-proxy", [revision(1)], limits="64Mi")],
        pods=[pod("redis-access-proxy-abc", "redis-access-proxy")],
        probe=1,
    )

    report, store, _, _ = run_case(results, proposer=proposer)

    assert report.status is InvestigationStatus.COMPLETED
    assert "新增工作负载" in report.conclusion
    assert store.events("baseline.rule")[0].payload["rule"] == "new-workload"
    assert proposer.calls == []
    assert report.proposal_id is None


def test_dependency_errors_without_restarts_stop_without_action():
    proposer = RecordingProposer()
    results = plan(
        deployments=[deployment("redis-cart", [revision(1, created=START - timedelta(hours=2))])],
        pods=[pod("redis-cart-abc", "redis-cart")],
        logs=[{"pod": "frontend-abc", "lines": ["GET /cart 500 dial tcp: connection refused"]}],
        probe=0,
    )

    report, store, _, _ = run_case(results, proposer=proposer)

    assert report.status is InvestigationStatus.COMPLETED
    assert "依赖访问错误" in report.conclusion
    assert store.events("baseline.rule")[0].payload["rule"] == "dependency-chain"
    assert proposer.calls == []


def test_oom_outside_the_window_is_ignored():
    results = plan(
        deployments=[
            deployment("paymentservice", [revision(1, created=START - timedelta(hours=2))])
        ],
        pods=[
            pod(
                "paymentservice-abc",
                "paymentservice",
                oom=True,
                restarts=1,
                finished=START - timedelta(minutes=50),
            )
        ],
        probe=1,
    )

    report, _, _, _ = run_case(results)

    assert report.status is InvestigationStatus.INCONCLUSIVE


def test_no_rule_hit_stays_inconclusive():
    results = plan(
        deployments=[deployment("redis-cart", [revision(1, created=START - timedelta(hours=2))])],
        pods=[pod("redis-cart-abc", "redis-cart")],
        probe=1,
    )

    report, store, _, _ = run_case(results)

    assert report.status is InvestigationStatus.INCONCLUSIVE
    assert report.conclusion is None
    assert "固定规则未命中" in report.termination_reason
    assert store.events("baseline.rule") == []


def test_failed_query_is_not_evidence():
    results = [
        result({"items": []}, error=ToolError(code="timeout", message="查询超时", retryable=True)),
        result({"items": []}),
        result({"items": []}),
        result({"items": []}, error=ToolError(code="timeout", message="查询超时")),
        result({"series": []}, source="prometheus"),
        result({"items": []}, source="knowledge"),
    ]

    report, store, toolbox, _ = run_case(results)

    assert report.status is InvestigationStatus.INCONCLUSIVE
    assert store.calls[0].error_code == "timeout"
    assert len(store.evidence) == 4
    assert all(item.raw_ref for item in store.evidence)


def test_tool_budget_exhaustion_aborts():
    results = plan(deployments=[deployment("redis-cart", [revision(1)])])

    report, store, toolbox, _ = run_case(results, budget_tool_calls=1)

    assert report.status is InvestigationStatus.ABORTED
    assert "预算耗尽：tool_calls" in report.termination_reason
    assert len(toolbox.requests) == 1
    assert len(store.calls) == 1
