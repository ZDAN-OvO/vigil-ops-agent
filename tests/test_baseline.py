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


def result(data, *, source="kubernetes", error=None, truncated=False):
    return ToolResult(
        tool_call_id=f"tc-{uuid4().hex[:8]}",
        source=source,
        query={},
        time_range=WINDOW,
        collected_at=START,
        data=None if error else data,
        error=error,
        raw_ref=None if error else "evidence/synthetic.json",
        truncated=truncated,
    )


def revision(
    number, *, image="repo/app:1", memory="128Mi", requests=None, replicas=1, created=None
):
    return {
        "revision": number,
        "image": image,
        "memory_limit": memory,
        "memory_request": requests or memory,
        "replicas": replicas,
        "ready_replicas": replicas,
        "created_at": (created or START + timedelta(minutes=5)).isoformat(),
    }


def deployment(
    name, revisions, *, limits="128Mi", app=None, selector=None, image="repo/app:1", requests=None
):
    return {
        "name": name,
        "labels": {"app": app or name},
        "selector": selector if selector is not None else {"app": app or name},
        "containers": [
            {
                "name": "server",
                "image": image,
                "limits": {"memory": limits},
                "requests": {"memory": requests or limits},
            }
        ],
        "revisions": revisions,
    }


def pod(name, app, *, oom=False, restarts=0, ready=True, finished=None, started=None, labels=None):
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
    pod_labels = {"app": app}
    pod_labels.update(labels or {})
    return {"name": name, "labels": pod_labels, "containers": [container]}


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


def build(*, budget_tool_calls=16, budget_wall_seconds=120, proposer=None):
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
            max_wall_clock_seconds=budget_wall_seconds,
        ),
        started_at=START,
    )
    return incident, investigation


def run_case(
    results,
    *,
    budget_tool_calls=16,
    budget_wall_seconds=120,
    proposer=None,
    toolbox=None,
    store_factory=MemoryStore,
    tool_timeout_seconds=10.0,
):
    incident, investigation = build(
        budget_tool_calls=budget_tool_calls,
        budget_wall_seconds=budget_wall_seconds,
        proposer=proposer,
    )
    store = store_factory(investigation)
    toolbox = toolbox or ScriptedToolbox(results)
    baseline = RulesBaseline(
        store=store,
        toolbox=toolbox,
        incident=incident,
        investigation=investigation,
        observation_window=WINDOW,
        runtime_path=None,
        proposer=proposer,
        tool_timeout_seconds=tool_timeout_seconds,
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
                image="repo/app:canary",
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
    assert "当前模板 revision 21" in report.conclusion
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
        pods=[pod("redis-cart-abc", "redis-cart", started=START - timedelta(hours=1))],
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
        pods=[pod("redis-cart-abc", "redis-cart", started=START - timedelta(hours=1))],
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


class SlowToolbox:
    def __init__(self, delay):
        self.delay = delay
        self.requests = []

    async def run(self, context, time_range, request):
        self.requests.append(request)
        await asyncio.sleep(self.delay)
        return result({"items": []})


def test_pod_prefix_does_not_attach_to_another_deployment():
    proposer = RecordingProposer()
    results = plan(
        deployments=[
            deployment(
                "cart", [revision(1, image="repo/cart:1"), revision(2, image="repo/cart:2")]
            ),
            deployment(
                "cart-worker",
                [revision(1, image="repo/worker:1"), revision(2, image="repo/worker:2")],
            ),
        ],
        pods=[pod("cart-worker-abc", "cart-worker", ready=False)],
        probe=1,
    )

    report, store, _, _ = run_case(results, proposer=proposer)

    assert report.status is InvestigationStatus.COMPLETED
    assert store.events("baseline.rule")[0].payload["deployment"] == "cart-worker"
    assert proposer.calls[0]["deployment"] == "cart-worker"


def test_oom_after_raising_the_limit_is_not_rolled_back():
    proposer = RecordingProposer()
    results = plan(
        deployments=[
            deployment(
                "currencyservice",
                [revision(1, memory="128Mi"), revision(2, memory="256Mi")],
                limits="256Mi",
            )
        ],
        pods=[pod("currencyservice-abc", "currencyservice", oom=True, restarts=2, ready=False)],
        probe=0,
    )

    report, store, _, _ = run_case(results, proposer=proposer)

    assert report.status is InvestigationStatus.COMPLETED
    assert store.events("baseline.rule")[0].payload["rule"] == "oom-limit"
    assert store.events("baseline.rule")[0].payload["action"] == "none"
    assert proposer.calls == []
    assert report.proposal_id is None


def test_template_created_before_window_is_not_a_recent_rollout():
    old = START - timedelta(days=5)
    results = plan(
        deployments=[
            deployment(
                "productcatalogservice",
                [
                    revision(1, image="repo/a", created=old),
                    revision(2, image="repo/b", created=old),
                ],
            )
        ],
        pods=[
            pod(
                "productcatalogservice-abc",
                "productcatalogservice",
                restarts=2,
                ready=False,
            )
        ],
        logs=[
            {
                "pod": "frontend-abc",
                "lines": ['{"message":"productcatalogservice returned error code 13"}'],
            }
        ],
        probe=1,
    )

    report, store, _, _ = run_case(results)

    assert report.status is InvestigationStatus.INCONCLUSIVE
    assert store.events("baseline.rule") == []


def test_reactivated_old_revision_with_fresh_pod_is_recent_rollout():
    old = START - timedelta(days=5)
    proposer = RecordingProposer()
    results = plan(
        deployments=[
            deployment(
                "productcatalogservice",
                [
                    revision(1, image="repo/a", created=old),
                    revision(2, image="repo/b", created=old),
                ],
            )
        ],
        pods=[pod("productcatalogservice-abc", "productcatalogservice")],
        logs=[
            {
                "pod": "frontend-abc",
                "lines": ['{"message":"productcatalogservice returned error code 13"}'],
            }
        ],
        probe=1,
    )

    report, store, _, _ = run_case(results, proposer=proposer)

    assert report.status is InvestigationStatus.COMPLETED
    assert store.events("baseline.rule")[0].payload["rule"] == "recent-rollout"
    assert proposer.calls[0]["deployment"] == "productcatalogservice"


def test_probe_failure_with_missing_workload_data_is_inconclusive():
    results = [
        result({"items": []}, error=ToolError(code="timeout", message="查询超时")),
        result({"items": []}, error=ToolError(code="timeout", message="查询超时")),
        result({"items": []}),
        result({"items": []}),
        result({"series": [{"labels": {}, "points": [[0, 0]]}]}, source="prometheus"),
        result({"items": []}, source="knowledge"),
    ]

    report, store, _, _ = run_case(results)

    assert report.status is InvestigationStatus.INCONCLUSIVE
    assert store.events("baseline.rule") == []


def test_wall_clock_overrun_aborts_without_proposal():
    proposer = RecordingProposer()
    toolbox = SlowToolbox(1.2)

    report, store, _, _ = run_case([], budget_wall_seconds=1, proposer=proposer, toolbox=toolbox)

    assert report.status is InvestigationStatus.ABORTED
    assert "预算耗尽：wall_clock" in report.termination_reason
    assert proposer.calls == []
    assert report.proposal_id is None


def test_log_hint_is_bound_to_the_proposal():
    proposer = RecordingProposer()
    results = plan(
        deployments=[
            deployment(
                "productcatalogservice",
                [revision(20), revision(21, image="repo/app:canary")],
            )
        ],
        pods=[pod("productcatalogservice-abc", "productcatalogservice")],
        logs=[{"pod": "frontend-abc", "lines": ['{"message":"productcatalogservice failed"}']}],
        probe=1,
    )

    report, store, _, _ = run_case(results, proposer=proposer)

    log_evidence = [
        item for item in store.evidence if "productcatalogservice failed" in item.observation
    ]
    assert log_evidence
    assert log_evidence[0].relation == "supporting"
    assert log_evidence[0].id in proposer.calls[0]["evidence_ids"]


def test_single_revision_reactivated_is_not_new_workload():
    old = START - timedelta(days=2)
    results = plan(
        deployments=[deployment("cartservice", [revision(1, created=old)])],
        pods=[pod("cartservice-abc", "cartservice")],
        probe=1,
    )

    report, store, _, _ = run_case(results)

    assert report.status is InvestigationStatus.INCONCLUSIVE
    assert store.events("baseline.rule") == []


def test_job_pod_label_mismatch_is_not_attributed_to_deployment():
    proposer = RecordingProposer()
    results = plan(
        deployments=[
            deployment(
                "cart",
                [revision(1, memory="128Mi"), revision(2, memory="16Mi")],
                limits="16Mi",
                selector={"app": "cart", "role": "api"},
            )
        ],
        pods=[
            pod(
                "cart-job-x",
                "cart",
                oom=True,
                restarts=1,
                ready=False,
                labels={"role": "worker"},
            )
        ],
        probe=0,
    )

    report, store, _, _ = run_case(results, proposer=proposer)

    # 同名 Job 的 Pod 不满足 Deployment 选择器，不能被归给 cart 并据此回退。
    assert report.status is InvestigationStatus.COMPLETED
    assert store.events("baseline.rule")[0].payload["deployment"] == ""
    assert proposer.calls == []


def test_oom_finished_after_window_is_not_a_hit():
    results = plan(
        deployments=[
            deployment(
                "currencyservice",
                [revision(1, created=START - timedelta(days=2))],
                limits="16Mi",
            )
        ],
        pods=[
            pod(
                "currencyservice-abc",
                "currencyservice",
                oom=True,
                started=START - timedelta(hours=1),
                finished=START + timedelta(minutes=20),
            )
        ],
        probe=1,
    )

    report, store, _, _ = run_case(results)

    assert report.status is InvestigationStatus.INCONCLUSIVE
    assert store.events("baseline.rule") == []


def test_template_created_after_window_is_not_a_recent_rollout():
    results = plan(
        deployments=[
            deployment(
                "productcatalogservice",
                [
                    revision(20, created=START + timedelta(minutes=16)),
                    revision(21, image="repo/app:canary", created=START + timedelta(minutes=16)),
                ],
            )
        ],
        pods=[pod("productcatalogservice-abc", "productcatalogservice", ready=False)],
        probe=1,
    )

    report, store, _, _ = run_case(results)

    # 模板创建时间落在观察窗之后属于数据不一致，不能当作窗内变更。
    assert report.status is InvestigationStatus.INCONCLUSIVE
    assert store.events("baseline.rule") == []


def test_single_tool_timeout_is_recorded_without_aborting():
    toolbox = SlowToolbox(0.5)

    report, store, _, _ = run_case([], toolbox=toolbox, tool_timeout_seconds=0.1)

    # 单次工具超时是一次查询失败，不是全局墙钟耗尽；六次调用都要记账。
    assert report.status is InvestigationStatus.INCONCLUSIVE
    assert "预算耗尽" not in report.termination_reason
    assert len(store.calls) == 6
    assert [call.error_code for call in store.calls] == ["timeout"] * 6
    assert store.usage["tool_calls"] == 6
    assert store.evidence == []
    assert store.events("baseline.rule") == []


def test_truncated_datasets_cannot_claim_complete_coverage():
    results = plan(deployments=[], pods=[], probe=0)
    results[0] = result({"items": [], "count": 0}, truncated=True)

    report, store, _, _ = run_case(results)

    # 数据集被截断时不能声称已排除其他根因，也不能只用探针下依赖结论。
    assert report.status is InvestigationStatus.INCONCLUSIVE
    assert store.events("baseline.rule") == []


def test_naive_template_timestamp_does_not_break_decision():
    proposer = RecordingProposer()
    latest = revision(21, image="repo/app:canary")
    latest["created_at"] = "2026-11-06T10:05:00"
    results = plan(
        deployments=[deployment("productcatalogservice", [revision(20), latest])],
        pods=[pod("productcatalogservice-abc", "productcatalogservice", ready=False)],
        probe=1,
    )

    report, _, _, _ = run_case(results, proposer=proposer)

    assert report.status is InvestigationStatus.COMPLETED
    assert proposer.calls[0]["deployment"] == "productcatalogservice"


class SlowStatusStore(MemoryStore):
    def __init__(self, investigation, delay):
        super().__init__(investigation)
        self.delay = delay

    async def set_hypothesis_status(self, hypothesis_id, status):
        await asyncio.sleep(self.delay)
        await super().set_hypothesis_status(hypothesis_id, status)


def test_wall_clock_exhausted_during_apply_stops_before_proposal():
    proposer = RecordingProposer()
    results = plan(
        deployments=[
            deployment(
                "currencyservice",
                [revision(1, memory="128Mi"), revision(2, memory="16Mi")],
                limits="16Mi",
            )
        ],
        pods=[pod("currencyservice-abc", "currencyservice", oom=True, restarts=4, ready=False)],
        probe=0,
    )

    report, store, _, _ = run_case(
        results,
        budget_wall_seconds=1,
        proposer=proposer,
        store_factory=lambda investigation: SlowStatusStore(investigation, 0.5),
    )

    # 记账写入耗尽墙钟后不能再提案，但已经写入的规则判定要保留。
    assert report.status is InvestigationStatus.ABORTED
    assert "预算耗尽：wall_clock" in report.termination_reason
    assert proposer.calls == []
    assert report.proposal_id is None
    assert len(store.events("baseline.rule")) == 1


def test_pod_started_after_window_is_not_a_recent_rollout():
    results = plan(
        deployments=[
            deployment(
                "productcatalogservice",
                [revision(20), revision(21, image="repo/app:canary")],
            )
        ],
        pods=[
            pod(
                "productcatalogservice-abc",
                "productcatalogservice",
                ready=False,
                started=START + timedelta(minutes=20),
            )
        ],
        probe=1,
    )

    report, store, _, _ = run_case(results)

    assert report.status is InvestigationStatus.INCONCLUSIVE
    assert store.events("baseline.rule") == []


def test_later_sidecar_start_does_not_hide_in_window_rollout():
    proposer = RecordingProposer()
    running = pod(
        "productcatalogservice-abc",
        "productcatalogservice",
        ready=False,
        started=START + timedelta(minutes=14, seconds=30),
    )
    running["containers"].append(
        {
            "name": "sidecar",
            "ready": True,
            "restart_count": 0,
            "started_at": (START + timedelta(minutes=15, seconds=1)).isoformat(),
        }
    )
    results = plan(
        deployments=[
            deployment(
                "productcatalogservice",
                [revision(20), revision(21, image="repo/app:canary")],
            )
        ],
        pods=[running],
        probe=1,
    )

    report, _, _, _ = run_case(results, proposer=proposer)

    # 主容器在窗内启动，sidecar 晚一秒启动不能把整次发布移出观察窗。
    assert report.status is InvestigationStatus.COMPLETED
    assert proposer.calls[0]["deployment"] == "productcatalogservice"


def test_oom_after_rollback_uses_the_live_template_as_current():
    proposer = RecordingProposer()
    results = plan(
        deployments=[
            deployment(
                "productcatalogservice",
                [
                    revision(16, memory="64Mi"),
                    revision(56, image="repo/app:canary", memory="16Mi"),
                    revision(57, memory="128Mi"),
                ],
                limits="16Mi",
                image="repo/app:canary",
            )
        ],
        pods=[pod("productcatalogservice-abc", "productcatalogservice", oom=True, restarts=3)],
        probe=0,
    )

    report, store, _, _ = run_case(results, proposer=proposer)

    # 回退到 revision 56 后，revision 57 的 ReplicaSet 已不在用；
    # 当前模板是 16Mi，上一版 128Mi，回退方向成立。
    assert report.status is InvestigationStatus.COMPLETED
    assert "上一版 revision 57" in report.conclusion
    assert proposer.calls[0]["deployment"] == "productcatalogservice"


def test_oom_selection_uses_the_latest_termination():
    results = plan(
        deployments=[
            deployment("svc-a", [revision(1, memory="128Mi"), revision(2, memory="16Mi")]),
            deployment("svc-b", [revision(1, memory="128Mi"), revision(2, memory="16Mi")]),
        ],
        pods=[
            pod(
                "svc-a-abc",
                "svc-a",
                oom=True,
                restarts=1,
                finished=START + timedelta(minutes=5),
            ),
            pod(
                "svc-b-abc",
                "svc-b",
                oom=True,
                restarts=1,
                finished=START + timedelta(minutes=12),
            ),
        ],
        probe=0,
    )

    report, store, _, _ = run_case(results)

    assert report.status is InvestigationStatus.COMPLETED
    assert store.events("baseline.rule")[0].payload["deployment"] == "svc-b"


def test_short_deployment_name_matches_log_hint():
    proposer = RecordingProposer()
    results = plan(
        deployments=[
            deployment("cart", [revision(1, image="repo/cart:1"), revision(2, image="repo/cart:2")])
        ],
        pods=[pod("cart-abc", "cart")],
        logs=[{"pod": "frontend-abc", "lines": ['{"message":"connection refused to cart"}']}],
        probe=1,
    )

    report, store, _, _ = run_case(results, proposer=proposer)

    assert report.status is InvestigationStatus.COMPLETED
    assert proposer.calls[0]["deployment"] == "cart"


def test_template_identity_includes_memory_request():
    proposer = RecordingProposer()
    results = plan(
        deployments=[
            deployment(
                "productcatalogservice",
                [
                    revision(1, memory="128Mi", requests="64Mi", replicas=3),
                    revision(2, memory="128Mi", requests="128Mi", replicas=1),
                ],
                limits="128Mi",
            )
        ],
        pods=[pod("productcatalogservice-abc", "productcatalogservice", ready=False)],
        probe=1,
    )

    report, store, _, _ = run_case(results, proposer=proposer)

    # 镜像和限额相同、请求不同，副本数更多的是旧版；实时模板按请求精确匹配新版。
    assert report.status is InvestigationStatus.COMPLETED
    assert "当前模板 revision 2" in report.conclusion
    assert proposer.calls[0]["deployment"] == "productcatalogservice"


def test_live_limit_mismatch_does_not_propose_a_rollback():
    proposer = RecordingProposer()
    results = plan(
        deployments=[
            deployment(
                "productcatalogservice",
                [revision(1, memory="512Mi"), revision(2, memory="128Mi")],
                limits="256Mi",
            )
        ],
        pods=[pod("productcatalogservice-abc", "productcatalogservice", oom=True, restarts=2)],
        probe=0,
    )

    report, store, _, _ = run_case(results, proposer=proposer)

    # 实时模板 256Mi 与历史模板都不一致，不能拿 128Mi 当当前版本判断回退方向。
    assert report.status is InvestigationStatus.COMPLETED
    assert "实时模板的内存限额与历史模板不一致" in report.conclusion
    assert proposer.calls == []


def test_missing_live_limit_does_not_propose_a_capped_rollback():
    proposer = RecordingProposer()
    results = plan(
        deployments=[
            deployment(
                "productcatalogservice",
                [revision(1, memory="128Mi"), revision(2, memory="16Mi")],
                limits=None,
                requests="8Mi",
            )
        ],
        pods=[pod("productcatalogservice-abc", "productcatalogservice", oom=True, restarts=1)],
        probe=0,
    )

    report, store, _, _ = run_case(results, proposer=proposer)

    # 实时模板没有限额，历史模板 16Mi/128Mi 都不是当前模板；回退等于新加一个上限。
    assert report.status is InvestigationStatus.COMPLETED
    assert "实时模板的内存限额与历史模板不一致" in report.conclusion
    assert proposer.calls == []
