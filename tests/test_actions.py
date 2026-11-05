"""受控回退：规则决定审批，执行前再校验，未知结果不重放。"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest

from aiops.actions.executor import (
    ActionExecutor,
    AutoExecutionError,
    _initial_status,
    proposal_hash,
)
from aiops.actions.policy import decide
from aiops.actions.restart_pod import PodPlan, PodRestartAdapter
from aiops.actions.rollout_undo import ActionError, RolloutPlan
from aiops.config import Settings
from aiops.contracts import (
    ActionKind,
    ActionOrigin,
    ActionProposal,
    ActionTarget,
    Approval,
    ApprovalDecision,
    ProposalStatus,
    RiskDecision,
    RiskDisposition,
    TimeRange,
    ToolResult,
)
from aiops.db import Store

DSN = Settings().database_url
pytestmark = pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL，跳过真实库测试")

START = datetime.now(UTC)
WINDOW = TimeRange(start=START, end=START + timedelta(minutes=1))
# 测试提案的统一标记；清理只删带这个标记的记录，避免误删并发创建的真实提案。
TEST_REASON = "测试：Pod 级状态损坏"


def make_proposal(**overrides) -> ActionProposal:
    values = {
        "id": "act-test",
        "kind": ActionKind.ROLLOUT_UNDO,
        "target": ActionTarget(
            cluster="vigil-lab",
            namespace="vigil-shop",
            kind="Deployment",
            name="productcatalogservice",
            uid="uid-1",
            resource_version="10",
        ),
        "parameters": {"from_revision": 2, "to_revision": 1},
        "idempotency_key": "uid-1:2",
        "reason": "坏发布",
        "origin": ActionOrigin.OPERATOR,
        "created_at": START,
    }
    values.update(overrides)
    return ActionProposal(**values)


def test_policy_requires_approval_for_lab_rollback():
    decision = decide(make_proposal(), target_busy=False)

    assert decision.disposition is RiskDisposition.APPROVAL
    assert decision.policy_version


def test_policy_denies_outside_allowed_namespace():
    proposal = make_proposal(
        target=ActionTarget(
            cluster="vigil-lab",
            namespace="kube-system",
            kind="Deployment",
            name="coredns",
            uid="uid-2",
            resource_version="1",
        )
    )
    decision = decide(proposal, target_busy=False)

    assert decision.disposition is RiskDisposition.DENY
    assert any("白名单" in reason for reason in decision.reasons)


def test_policy_denies_busy_target():
    busy = decide(make_proposal(), target_busy=True)

    assert busy.disposition is RiskDisposition.DENY
    assert any("未完成的执行" in reason for reason in busy.reasons)


def test_model_proposal_cannot_be_created_without_evidence():
    with pytest.raises(ValueError):
        make_proposal(origin=ActionOrigin.MODEL)


def test_proposal_hash_changes_with_target():
    first = make_proposal()
    second = make_proposal(parameters={"from_revision": 3, "to_revision": 1})

    assert proposal_hash(first) == proposal_hash(make_proposal())
    assert proposal_hash(first) != proposal_hash(second)


class FakeAdapter:
    def __init__(self, plan, *, fail_on_apply=False, runtime=None):
        self._plan = plan
        self._fail_on_apply = fail_on_apply
        self._runtime = runtime
        self.calls = []

    def plan(self, namespace, name):
        return self._plan

    def apply(self, plan, *, dry_run, proposal_id):
        self.calls.append(dry_run)
        if self._fail_on_apply and not dry_run:
            raise RuntimeError("连接中断")
        return {"revision": plan.target_revision, "dry_run": dry_run}

    def wait_rolled_out(self, plan, timeout_seconds):
        return True

    def state(self, namespace, name):
        if self._runtime is not None:
            return self._runtime
        return {
            "memory_limit": self._plan.target_memory_limit,
            "pods": [{"name": "pod-new", "ready": True, "restarts": 0}],
        }


class FakePrometheus:
    def __init__(self, values):
        self._values = values

    async def query_range(self, context, expression, time_range, *, step_seconds):
        return ToolResult(
            tool_call_id=f"call-{uuid4().hex[:8]}",
            source="prometheus",
            query={"expression": expression},
            time_range=time_range,
            collected_at=datetime.now(UTC),
            data={
                "series": [
                    {
                        "labels": {},
                        "points": [
                            [float(index), value] for index, value in enumerate(self._values)
                        ],
                    }
                ]
            },
            raw_ref="evidence/synthetic/verify.json",
        )


def rollout_plan(*, current_memory_limit=None, target_memory_limit=None):
    return RolloutPlan(
        namespace="vigil-shop",
        name="productcatalogservice",
        current_revision=2,
        target_revision=1,
        current_image="vigil/productcatalogservice-bad:exp",
        target_image="gcr.io/productcatalogservice@sha256:good",
        template={"metadata": {}, "spec": {"containers": [{"name": "server"}]}},
        deployment_uid="uid-1",
        resource_version="10",
        current_memory_limit=current_memory_limit,
        target_memory_limit=target_memory_limit,
    )


@pytest.fixture
def cleanup():
    ids: list[str] = []
    started_at = datetime.now(UTC)
    yield ids
    with psycopg.connect(DSN) as conn:
        # 失败路径可能来不及登记提案 ID；实验命名空间里本用例开始后的提案都属于测试。
        rows = conn.execute(
            """
            SELECT id FROM action_proposals
             WHERE namespace = %s AND created_at >= %s AND reason = %s
            """,
            ("vigil-experiment", started_at, TEST_REASON),
        ).fetchall()
        for proposal_id in {row[0] for row in rows} | set(ids):
            conn.execute("DELETE FROM action_locks WHERE proposal_id = %s", (proposal_id,))
            conn.execute("DELETE FROM action_attempts WHERE proposal_id = %s", (proposal_id,))
            conn.execute("DELETE FROM approvals WHERE proposal_id = %s", (proposal_id,))
            conn.execute("DELETE FROM audit_events WHERE correlation_id = %s", (proposal_id,))
            conn.execute("DELETE FROM action_proposals WHERE id = %s", (proposal_id,))


def build_executor(
    store,
    adapter,
    prometheus,
    tmp_path,
    *,
    approval_ttl_seconds=900,
    restart_adapter=None,
    verify_seconds=60,
    sample_seconds=15,
    cluster="vigil-lab",
):
    return ActionExecutor(
        store=store,
        adapter=adapter,
        restart_adapter=restart_adapter,
        prometheus=prometheus,
        runtime_path=tmp_path,
        cluster=cluster,
        namespace="vigil-shop",
        verify_seconds=verify_seconds,
        sample_seconds=sample_seconds,
        approval_ttl_seconds=approval_ttl_seconds,
        verify_delay_seconds=0,
        verify_grace_seconds=0,
    )


async def propose_and_approve(adapter, prometheus, tmp_path, cleanup):
    async with Store.open(DSN) as store:
        await store.migrate()
        executor = build_executor(store, adapter, prometheus, tmp_path)
        proposal, decision = await executor.propose_rollback(
            deployment="productcatalogservice", reason="确认坏发布", actor="tester"
        )
        cleanup.append(proposal.id)
        attempt = await executor.approve(proposal.id, actor="tester", reason="同意")
        saved = await store.get_proposal(proposal.id)
        approval = await store.get_approval(proposal.id)
    return proposal, decision, attempt, saved, approval


def test_proposal_waits_for_approval_then_verifies_recovery(tmp_path, cleanup):
    adapter = FakeAdapter(rollout_plan())
    proposal, decision, attempt, saved, approval = asyncio.run(
        propose_and_approve(adapter, FakePrometheus([1.0, 1.0, 1.0, 1.0]), tmp_path, cleanup)
    )

    assert decision.disposition is RiskDisposition.APPROVAL
    assert proposal.status is ProposalStatus.PENDING_APPROVAL
    assert saved is not None and saved.status is ProposalStatus.SUCCEEDED
    assert approval is not None and approval.proposal_hash
    assert attempt.status.value == "verified"
    assert attempt.verification["ok"] is True
    assert adapter.calls == [True, False]


def test_failed_business_verification_is_not_success(tmp_path, cleanup):
    adapter = FakeAdapter(rollout_plan())
    _, _, attempt, saved, _ = asyncio.run(
        propose_and_approve(adapter, FakePrometheus([1.0, 1.0, 0.0, 0.0]), tmp_path, cleanup)
    )

    assert attempt.status.value == "failed"
    assert saved.status is ProposalStatus.FAILED
    assert attempt.verification["tail_failures"] == 2


def test_missing_data_cannot_count_as_recovery(tmp_path, cleanup):
    adapter = FakeAdapter(rollout_plan())
    _, _, attempt, saved, _ = asyncio.run(
        propose_and_approve(adapter, FakePrometheus([]), tmp_path, cleanup)
    )

    assert attempt.status.value == "failed"
    assert attempt.verification["reason"] == "insufficient_data"
    assert saved.status is ProposalStatus.FAILED


def test_unknown_apply_result_stops_without_replay(tmp_path, cleanup):
    adapter = FakeAdapter(rollout_plan(), fail_on_apply=True)
    _, _, attempt, saved, _ = asyncio.run(
        propose_and_approve(adapter, FakePrometheus([1.0, 1.0]), tmp_path, cleanup)
    )

    assert attempt.status.value == "unknown"
    assert saved.status is ProposalStatus.UNKNOWN
    assert adapter.calls == [True, False]


def test_drift_after_approval_blocks_execution(tmp_path, cleanup):
    class DriftingAdapter(FakeAdapter):
        def __init__(self, plan):
            super().__init__(plan)
            self._calls = 0

        def plan(self, namespace, name):
            self._calls += 1
            if self._calls == 1:
                return self._plan
            return RolloutPlan(
                namespace=self._plan.namespace,
                name=self._plan.name,
                current_revision=3,
                target_revision=2,
                current_image="other",
                target_image="other-2",
                template={},
                deployment_uid="uid-1",
                resource_version="11",
            )

    adapter = DriftingAdapter(rollout_plan())

    async def scenario():
        async with Store.open(DSN) as store:
            await store.migrate()
            executor = build_executor(store, adapter, FakePrometheus([1.0, 1.0]), tmp_path)
            proposal, _ = await executor.propose_rollback(
                deployment="productcatalogservice", reason="坏发布", actor="tester"
            )
            cleanup.append(proposal.id)
            with pytest.raises(ActionError) as caught:
                await executor.approve(proposal.id, actor="tester")
            saved = await store.get_proposal(proposal.id)
            attempt = await store.get_attempt(proposal.id)
        return caught.value, saved, attempt

    error, saved, attempt = asyncio.run(scenario())
    assert error.code == "drift"
    assert saved.status is ProposalStatus.FAILED
    assert attempt is None
    assert adapter.calls == []


def test_second_approval_is_refused(tmp_path, cleanup):
    adapter = FakeAdapter(rollout_plan())

    async def scenario():
        async with Store.open(DSN) as store:
            await store.migrate()
            executor = build_executor(store, adapter, FakePrometheus([1.0, 1.0]), tmp_path)
            proposal, _ = await executor.propose_rollback(
                deployment="productcatalogservice", reason="坏发布", actor="tester"
            )
            cleanup.append(proposal.id)
            await executor.approve(proposal.id, actor="tester")
            with pytest.raises(ActionError) as caught:
                await executor.approve(proposal.id, actor="tester")
        return caught.value

    error = asyncio.run(scenario())
    assert error.code == "not_pending"


def test_approved_proposal_survives_restart_and_resumes(tmp_path, cleanup):
    adapter = FakeAdapter(rollout_plan())

    async def first_process():
        async with Store.open(DSN) as store:
            await store.migrate()
            executor = build_executor(store, adapter, FakePrometheus([1.0, 1.0]), tmp_path)
            proposal, _ = await executor.propose_rollback(
                deployment="productcatalogservice", reason="坏发布", actor="tester"
            )
            cleanup.append(proposal.id)
            deferred = await executor.approve(proposal.id, actor="tester", defer=True)
        return proposal.id, deferred

    async def second_process(proposal_id):
        async with Store.open(DSN) as store:
            await store.migrate()
            waiting = await store.list_proposals(status=ProposalStatus.APPROVED)
            executor = build_executor(store, adapter, FakePrometheus([1.0, 1.0]), tmp_path)
            attempt = await executor.resume(proposal_id, actor="restarted")
            saved = await store.get_proposal(proposal_id)
        return waiting, attempt, saved

    proposal_id, deferred = asyncio.run(first_process())
    waiting, attempt, saved = asyncio.run(second_process(proposal_id))

    assert deferred is None
    assert proposal_id in [item.id for item in waiting]
    assert attempt.status.value == "verified"
    assert saved.status is ProposalStatus.SUCCEEDED
    assert adapter.calls == [True, False]


def test_expired_approval_is_not_executed(tmp_path, cleanup):
    adapter = FakeAdapter(rollout_plan())

    async def scenario():
        async with Store.open(DSN) as store:
            await store.migrate()
            executor = build_executor(
                store, adapter, FakePrometheus([1.0, 1.0]), tmp_path, approval_ttl_seconds=0
            )
            proposal, _ = await executor.propose_rollback(
                deployment="productcatalogservice", reason="坏发布", actor="tester"
            )
            cleanup.append(proposal.id)
            await executor.approve(proposal.id, actor="tester", defer=True)
        async with Store.open(DSN) as store:
            executor = build_executor(
                store, adapter, FakePrometheus([1.0, 1.0]), tmp_path, approval_ttl_seconds=0
            )
            with pytest.raises(ActionError) as caught:
                await executor.resume(proposal.id, actor="restarted")
            saved = await store.get_proposal(proposal.id)
            attempt = await store.get_attempt(proposal.id)
        return caught.value, saved, attempt

    error, saved, attempt = asyncio.run(scenario())
    assert error.code == "expired"
    assert saved.status is ProposalStatus.EXPIRED
    assert attempt is None
    assert adapter.calls == []


def test_changed_proposal_does_not_reuse_approval(tmp_path, cleanup):
    adapter = FakeAdapter(rollout_plan())

    async def scenario():
        async with Store.open(DSN) as store:
            await store.migrate()
            executor = build_executor(store, adapter, FakePrometheus([1.0, 1.0]), tmp_path)
            proposal, _ = await executor.propose_rollback(
                deployment="productcatalogservice", reason="坏发布", actor="tester"
            )
            cleanup.append(proposal.id)
            await executor.approve(proposal.id, actor="tester", defer=True)
        with psycopg.connect(DSN) as conn:
            conn.execute(
                """UPDATE action_proposals
                      SET parameters = parameters || '{"to_revision": 0}'::jsonb
                    WHERE id = %s""",
                (proposal.id,),
            )
        async with Store.open(DSN) as store:
            executor = build_executor(store, adapter, FakePrometheus([1.0, 1.0]), tmp_path)
            with pytest.raises(ActionError) as caught:
                await executor.resume(proposal.id, actor="restarted")
            saved = await store.get_proposal(proposal.id)
        return caught.value, saved

    error, saved = asyncio.run(scenario())
    assert error.code == "hash_mismatch"
    assert saved.status is ProposalStatus.FAILED
    assert adapter.calls == []


def test_repeated_resume_executes_once(tmp_path, cleanup):
    adapter = FakeAdapter(rollout_plan())

    async def scenario():
        async with Store.open(DSN) as store:
            await store.migrate()
            executor = build_executor(store, adapter, FakePrometheus([1.0, 1.0]), tmp_path)
            proposal, _ = await executor.propose_rollback(
                deployment="productcatalogservice", reason="坏发布", actor="tester"
            )
            cleanup.append(proposal.id)
            await executor.approve(proposal.id, actor="tester", defer=True)
            first = await executor.resume(proposal.id, actor="restarted")
            with pytest.raises(ActionError) as caught:
                await executor.resume(proposal.id, actor="restarted")
        return first, caught.value

    first, error = asyncio.run(scenario())
    assert first.status.value == "verified"
    assert error.code == "already_executed"
    assert adapter.calls == [True, False]


def test_rejection_after_restart_never_touches_cluster(tmp_path, cleanup):
    adapter = FakeAdapter(rollout_plan())

    async def first_process():
        async with Store.open(DSN) as store:
            await store.migrate()
            executor = build_executor(store, adapter, FakePrometheus([1.0, 1.0]), tmp_path)
            proposal, _ = await executor.propose_rollback(
                deployment="productcatalogservice", reason="坏发布", actor="tester"
            )
            cleanup.append(proposal.id)
        return proposal.id

    async def second_process(proposal_id):
        async with Store.open(DSN) as store:
            await store.migrate()
            executor = build_executor(store, adapter, FakePrometheus([1.0, 1.0]), tmp_path)
            proposal = await executor.reject(proposal_id, actor="restarted", reason="不需要")
            attempt = await store.get_attempt(proposal_id)
        return proposal, attempt

    proposal_id = asyncio.run(first_process())
    proposal, attempt = asyncio.run(second_process(proposal_id))

    assert proposal.status is ProposalStatus.REJECTED
    assert attempt is None
    assert adapter.calls == []


def test_concurrent_approvals_only_one_wins(tmp_path, cleanup):
    adapter = FakeAdapter(rollout_plan())

    async def scenario():
        async with Store.open(DSN) as store_a, Store.open(DSN) as store_b:
            await store_a.migrate()
            executor_a = build_executor(store_a, adapter, FakePrometheus([1.0, 1.0]), tmp_path)
            proposal, _ = await executor_a.propose_rollback(
                deployment="productcatalogservice", reason="坏发布", actor="tester"
            )
            cleanup.append(proposal.id)
            executor_b = build_executor(store_b, adapter, FakePrometheus([1.0, 1.0]), tmp_path)
            results = await asyncio.gather(
                executor_a.approve(proposal.id, actor="alice", defer=True),
                executor_b.approve(proposal.id, actor="bob", defer=True),
                return_exceptions=True,
            )
        with psycopg.connect(DSN) as conn:
            count = conn.execute(
                "SELECT count(*) FROM approvals WHERE proposal_id = %s", (proposal.id,)
            ).fetchone()[0]
        return results, count

    results, count = asyncio.run(scenario())
    errors = [item for item in results if isinstance(item, ActionError)]
    assert len(errors) == 1 and errors[0].code == "not_pending"
    assert count == 1


def test_memory_limit_restored_is_part_of_verification(tmp_path, cleanup):
    adapter = FakeAdapter(rollout_plan(target_memory_limit="128Mi"))

    _, _, attempt, saved, _ = asyncio.run(
        propose_and_approve(adapter, FakePrometheus([1.0, 1.0, 1.0, 1.0]), tmp_path, cleanup)
    )

    assert attempt.status.value == "verified"
    assert attempt.verification["memory_limit_restored"] is True
    assert attempt.verification["runtime"]["memory_limit"] == "128Mi"
    assert saved.status is ProposalStatus.SUCCEEDED


def test_unrestored_memory_limit_is_not_recovery(tmp_path, cleanup):
    adapter = FakeAdapter(
        rollout_plan(target_memory_limit="128Mi"),
        runtime={"memory_limit": "16Mi", "pods": [{"ready": True, "restarts": 0}]},
    )

    _, _, attempt, saved, _ = asyncio.run(
        propose_and_approve(adapter, FakePrometheus([1.0, 1.0, 1.0, 1.0]), tmp_path, cleanup)
    )

    assert attempt.status.value == "failed"
    assert attempt.verification["ok"] is True
    assert attempt.verification["memory_limit_restored"] is False
    assert saved.status is ProposalStatus.FAILED


def test_pod_restarts_in_window_are_not_recovery(tmp_path, cleanup):
    adapter = FakeAdapter(
        rollout_plan(target_memory_limit="128Mi"),
        runtime={"memory_limit": "128Mi", "pods": [{"ready": True, "restarts": 2}]},
    )

    _, _, attempt, saved, _ = asyncio.run(
        propose_and_approve(adapter, FakePrometheus([1.0, 1.0, 1.0, 1.0]), tmp_path, cleanup)
    )

    assert attempt.status.value == "failed"
    assert attempt.verification["pod_restarts"] == 2
    assert saved.status is ProposalStatus.FAILED


def test_proposal_binds_memory_limits(tmp_path, cleanup):
    adapter = FakeAdapter(rollout_plan(current_memory_limit="16Mi", target_memory_limit="128Mi"))

    async def scenario():
        async with Store.open(DSN) as store:
            await store.migrate()
            executor = build_executor(store, adapter, FakePrometheus([1.0, 1.0]), tmp_path)
            proposal, _ = await executor.propose_rollback(
                deployment="productcatalogservice", reason="内存限额不足", actor="tester"
            )
            cleanup.append(proposal.id)
            saved = await store.get_proposal(proposal.id)
        return saved

    saved = asyncio.run(scenario())
    assert saved is not None
    assert saved.parameters["from_memory_limit"] == "16Mi"
    assert saved.parameters["to_memory_limit"] == "128Mi"


def pod_plan(**overrides) -> PodPlan:
    values = {
        "namespace": "vigil-shop",
        "name": "rebuild-demo-abc",
        "uid": "pod-uid-1",
        "resource_version": "100",
        "owner_kind": "ReplicaSet",
        "owner_name": "rebuild-demo-5f6",
        "owner_uid": "rs-uid-1",
        "template_hash": "tmpl-1",
        "spec_hash": "spec-1",
        "sibling_uids": (),
        "restart_count": 0,
        "ready": True,
        "image": "vigil/pod-rebuild-demo:20261105",
        "discardable": True,
        "ip": "10.42.0.9",
    }
    values.update(overrides)
    return PodPlan(**values)


class FakeRestartAdapter:
    def __init__(
        self,
        plan,
        *,
        rebuilt_spec_hash=None,
        fail_on_delete=False,
        replan=None,
        service_status=200,
        service_selects=True,
        service_selects_after=None,
        service_selects_not_ready=False,
        live_ready=True,
        live_restarts=0,
        rebuild_available=True,
        replan_error=None,
    ):
        self._plan = plan
        self._rebuilt_spec_hash = rebuilt_spec_hash or plan.spec_hash
        self._fail_on_delete = fail_on_delete
        self._replan = replan
        self._service_status = service_status
        self._service_selects = service_selects
        self._service_selects_after = service_selects_after
        self._service_selects_not_ready = service_selects_not_ready
        self._live_ready = live_ready
        self._live_restarts = live_restarts
        self._rebuild_available = rebuild_available
        self._replan_error = replan_error
        self.calls = []
        self._plan_calls = 0
        self._service_calls = 0

    def plan(self, namespace, name):
        self._plan_calls += 1
        if self._replan_error is not None and self._plan_calls > 1:
            raise self._replan_error
        if self._replan is not None and self._plan_calls > 1:
            return self._replan
        return self._plan

    def dry_run(self, plan):
        self.calls.append(("dry_run", plan.uid))
        return {"dry_run": True}

    def execute(self, plan):
        self.calls.append(("execute", plan.uid))
        if self._fail_on_delete:
            raise RuntimeError("连接中断")
        return {"deleted": plan.name, "uid": plan.uid}

    def wait_rebuilt(self, plan, timeout_seconds):
        self.calls.append(("wait", plan.uid))
        if not self._rebuild_available:
            return None
        return {
            "name": "rebuild-demo-def",
            "uid": "pod-uid-2",
            "ready": True,
            "restarts": 0,
            "template_hash": self._rebuilt_spec_hash,
            "ip": "10.42.0.9",
            "phase": "Running",
        }

    def pod_get(self, namespace, name, path):
        status = self._service_status
        return {"ok": status == 200, "status": status, "body": "ok" if status == 200 else ""}

    def service_selects(self, namespace, service, pod_ip, *, ready_only=True):
        self._service_calls += 1
        ok = self._service_selects
        if self._service_selects_not_ready:
            # 目标只出现在 notReadyAddresses：选择关系成立，但还没有就绪端点。
            ok = not ready_only
        if self._service_selects_after is not None and self._service_calls > 1:
            ok = self._service_selects_after
        return {"ok": ok, "pod_ip": pod_ip, "addresses": [pod_ip], "ready_only": ready_only}

    def pod_state(self, namespace, owner_uid, uid):
        return {
            "name": "rebuild-demo-def",
            "uid": uid,
            "ready": self._live_ready,
            "restarts": self._live_restarts,
            "template_hash": self._rebuilt_spec_hash,
            "ip": "10.42.0.9",
            "phase": "Running",
        }


def restart_proposal(**overrides) -> ActionProposal:
    values = {
        "kind": ActionKind.RESTART_POD,
        "target": ActionTarget(
            cluster="vigil-lab",
            namespace="vigil-shop",
            kind="Pod",
            name="rebuild-demo-abc",
            uid="pod-uid-1",
            resource_version="100",
        ),
        "parameters": {
            "pod": "rebuild-demo-abc",
            "replica_set": "rebuild-demo-5f6",
            "owner_kind": "ReplicaSet",
            "discardable": True,
            "template_hash": "tmpl-1",
            "service": "rebuild-demo",
            "path": "/work",
        },
        "idempotency_key": "restart:pod-uid-1",
        "reason": "Pod 级状态损坏",
    }
    values.update(overrides)
    return make_proposal(**values)


def test_restart_policy_allows_only_controlled_discardable_pod():
    decision = decide(restart_proposal(), target_busy=False)

    assert decision.disposition is RiskDisposition.APPROVAL


def experiment_restart_proposal() -> ActionProposal:
    proposal = restart_proposal()
    return proposal.model_copy(
        update={"target": proposal.target.model_copy(update={"namespace": "vigil-experiment"})}
    )


def test_restart_in_experiment_namespace_is_auto_allowed():
    decision = decide(experiment_restart_proposal(), target_busy=False)

    assert decision.disposition is RiskDisposition.AUTO
    assert any("自动执行规则" in reason for reason in decision.reasons)


@pytest.mark.parametrize(
    "override",
    [
        {"discardable": False},
        {"owner_kind": None},
        {"service": ""},
    ],
)
def test_experiment_namespace_auto_still_obeys_hard_constraints(override):
    proposal = experiment_restart_proposal()
    proposal = proposal.model_copy(update={"parameters": {**proposal.parameters, **override}})

    decision = decide(proposal, target_busy=False)

    assert decision.disposition is RiskDisposition.DENY


def test_experiment_namespace_busy_target_is_denied():
    decision = decide(experiment_restart_proposal(), target_busy=True)

    assert decision.disposition is RiskDisposition.DENY


def test_auto_decision_maps_to_approved_proposal_status():
    decision = RiskDecision(
        proposal_id="act-1",
        disposition=RiskDisposition.AUTO,
        policy_version="test",
        reasons=["规则"],
        requires_compensation=False,
    )

    assert _initial_status(decision) is ProposalStatus.APPROVED
    assert _initial_status(decision.model_copy(update={"disposition": RiskDisposition.DENY})) is (
        ProposalStatus.DENIED
    )
    assert (
        _initial_status(decision.model_copy(update={"disposition": RiskDisposition.APPROVAL}))
        is ProposalStatus.PENDING_APPROVAL
    )


def test_auto_restart_stops_after_a_failed_auto_execution():
    decision = decide(experiment_restart_proposal(), target_busy=False, prior_auto_failure=True)

    assert decision.disposition is RiskDisposition.APPROVAL
    assert any("上一次执行失败" in reason for reason in decision.reasons)


def test_rollback_in_experiment_namespace_still_needs_approval():
    proposal = make_proposal(
        target=ActionTarget(
            cluster="vigil-lab",
            namespace="vigil-experiment",
            kind="Deployment",
            name="rebuild-demo",
            uid="uid-3",
            resource_version="1",
        )
    )

    assert decide(proposal, target_busy=False).disposition is RiskDisposition.APPROVAL


def test_restart_outside_experiment_namespace_is_not_auto():
    proposal = restart_proposal()

    assert decide(proposal, target_busy=False).disposition is RiskDisposition.APPROVAL


def test_restart_policy_denies_pod_without_controller():
    proposal = restart_proposal()
    proposal = proposal.model_copy(
        update={"parameters": {**proposal.parameters, "owner_kind": None}}
    )

    decision = decide(proposal, target_busy=False)

    assert decision.disposition is RiskDisposition.DENY
    assert any("ReplicaSet" in reason for reason in decision.reasons)


def test_restart_policy_denies_pod_not_marked_discardable():
    proposal = restart_proposal()
    proposal = proposal.model_copy(
        update={"parameters": {**proposal.parameters, "discardable": False}}
    )

    assert decide(proposal, target_busy=False).disposition is RiskDisposition.DENY


def test_restart_policy_denies_missing_business_service():
    proposal = restart_proposal()
    proposal = proposal.model_copy(update={"parameters": {**proposal.parameters, "service": ""}})

    assert decide(proposal, target_busy=False).disposition is RiskDisposition.DENY


async def run_restart(
    adapter,
    tmp_path,
    cleanup,
    *,
    verify_seconds=1,
    sample_seconds=0.2,
    namespace="vigil-shop",
    approve=True,
    origin=ActionOrigin.OPERATOR,
    evidence_ids=None,
    cluster="vigil-lab",
):
    async with Store.open(DSN) as store:
        await store.migrate()
        executor = build_executor(
            store,
            FakeAdapter(rollout_plan()),
            FakePrometheus([1.0]),
            tmp_path,
            restart_adapter=adapter,
            verify_seconds=verify_seconds,
            sample_seconds=sample_seconds,
            cluster=cluster,
        )
        try:
            proposal, decision = await executor.propose_restart_pod(
                pod="rebuild-demo-abc",
                service="rebuild-demo",
                path="/work",
                reason=TEST_REASON,
                actor="tester",
                namespace=namespace,
                origin=origin,
                evidence_ids=evidence_ids,
            )
        except AutoExecutionError as exc:
            cleanup.append(exc.proposal_id)
            raise
        cleanup.append(proposal.id)
        attempt = await store.get_attempt(proposal.id)
        if approve:
            try:
                attempt = await executor.approve(proposal.id, actor="tester", reason="同意")
            except ActionError:
                attempt = None
        saved = await store.get_proposal(proposal.id)
    return proposal, decision, attempt, saved


def test_auto_restart_executes_without_human_approval(tmp_path, cleanup):
    adapter = FakeRestartAdapter(pod_plan(namespace="vigil-experiment"))

    proposal, decision, attempt, saved = asyncio.run(
        run_restart(adapter, tmp_path, cleanup, namespace="vigil-experiment", approve=False)
    )

    assert decision.disposition is RiskDisposition.AUTO
    assert attempt is not None
    assert attempt.status.value == "verified"
    assert saved.status is ProposalStatus.SUCCEEDED
    assert [call[0] for call in adapter.calls] == ["dry_run", "execute", "wait"]

    async def read():
        async with Store.open(DSN) as store:
            return (
                await store.get_approval(proposal.id),
                await store.list_audit(proposal.id),
            )

    approval, audit = asyncio.run(read())
    assert approval is not None
    assert approval.actor.startswith("policy:")
    assert approval.reason.startswith("自动允许")
    assert any(event.event == "action.auto_approved" for event in audit)
    assert any(event.actor.startswith("policy:") for event in audit)


def test_auto_restart_failure_is_not_reported_as_verified(tmp_path, cleanup):
    adapter = FakeRestartAdapter(pod_plan(namespace="vigil-experiment"), service_status=500)

    _, decision, attempt, saved = asyncio.run(
        run_restart(adapter, tmp_path, cleanup, namespace="vigil-experiment", approve=False)
    )

    assert decision.disposition is RiskDisposition.AUTO
    assert attempt.status.value == "failed"
    assert saved.status is ProposalStatus.FAILED


def test_model_proposal_in_experiment_namespace_is_auto_executed(tmp_path, cleanup):
    adapter = FakeRestartAdapter(pod_plan(namespace="vigil-experiment"))

    _, decision, attempt, saved = asyncio.run(
        run_restart(
            adapter,
            tmp_path,
            cleanup,
            namespace="vigil-experiment",
            approve=False,
            origin=ActionOrigin.MODEL,
            evidence_ids=["ev-1"],
        )
    )

    assert decision.disposition is RiskDisposition.AUTO
    assert attempt is not None
    assert attempt.status.value == "verified"
    assert saved.status is ProposalStatus.SUCCEEDED


def test_auto_restart_is_not_repeated_after_a_failed_auto_attempt(tmp_path, cleanup):
    first = FakeRestartAdapter(pod_plan(namespace="vigil-experiment"), service_status=500)
    _, decision, attempt, _ = asyncio.run(
        run_restart(first, tmp_path, cleanup, namespace="vigil-experiment", approve=False)
    )
    assert decision.disposition is RiskDisposition.AUTO
    assert attempt.status.value == "failed"

    second = FakeRestartAdapter(
        pod_plan(namespace="vigil-experiment", name="rebuild-demo-def", uid="pod-uid-3")
    )
    _, decision2, attempt2, saved2 = asyncio.run(
        run_restart(second, tmp_path, cleanup, namespace="vigil-experiment", approve=False)
    )

    assert decision2.disposition is RiskDisposition.APPROVAL
    assert attempt2 is None
    assert saved2.status is ProposalStatus.PENDING_APPROVAL
    assert second.calls == []


def test_auto_restart_stops_after_a_pre_delete_failure(tmp_path, cleanup):
    """删除前被拒也要留下停止状态，不能因为没写执行记录就再次自动放行。"""

    async def first_attempt():
        async with Store.open(DSN) as store:
            await store.migrate()
            executor = build_executor(
                store,
                FakeAdapter(rollout_plan()),
                FakePrometheus([1.0]),
                tmp_path,
                restart_adapter=FakeRestartAdapter(
                    pod_plan(namespace="vigil-experiment"), service_selects=False
                ),
                verify_seconds=1,
                sample_seconds=0.2,
            )
            with pytest.raises(AutoExecutionError) as info:
                await executor.propose_restart_pod(
                    pod="rebuild-demo-abc",
                    service="rebuild-demo",
                    path="/work",
                    reason=TEST_REASON,
                    actor="tester",
                    namespace="vigil-experiment",
                )
            return info.value.proposal_id

    cleanup.append(asyncio.run(first_attempt()))

    second = FakeRestartAdapter(
        pod_plan(namespace="vigil-experiment", name="rebuild-demo-def", uid="pod-uid-3")
    )
    _, decision, attempt, _ = asyncio.run(
        run_restart(second, tmp_path, cleanup, namespace="vigil-experiment", approve=False)
    )

    assert decision.disposition is RiskDisposition.APPROVAL
    assert attempt is None
    assert second.calls == []


def test_auto_stop_gate_survives_a_replica_set_change(tmp_path, cleanup):
    first = FakeRestartAdapter(
        pod_plan(
            namespace="vigil-experiment", owner_name="rebuild-demo-old", workload_uid="deploy-uid-1"
        ),
        service_status=500,
    )
    _, decision, attempt, _ = asyncio.run(
        run_restart(first, tmp_path, cleanup, namespace="vigil-experiment", approve=False)
    )
    assert decision.disposition is RiskDisposition.AUTO
    assert attempt.status.value == "failed"

    second = FakeRestartAdapter(
        pod_plan(
            namespace="vigil-experiment",
            name="rebuild-demo-def",
            uid="pod-uid-3",
            owner_name="rebuild-demo-new",
            workload_uid="deploy-uid-1",
        )
    )
    _, decision2, attempt2, _ = asyncio.run(
        run_restart(second, tmp_path, cleanup, namespace="vigil-experiment", approve=False)
    )

    assert decision2.disposition is RiskDisposition.APPROVAL
    assert attempt2 is None


def test_auto_stop_gate_is_scoped_to_one_cluster(tmp_path, cleanup):
    other = FakeRestartAdapter(pod_plan(namespace="vigil-experiment"), service_status=500)
    _, first, attempt, _ = asyncio.run(
        run_restart(
            other,
            tmp_path,
            cleanup,
            namespace="vigil-experiment",
            approve=False,
            cluster="other-cluster",
        )
    )
    assert first.disposition is RiskDisposition.AUTO
    assert attempt.status.value == "failed"

    second = FakeRestartAdapter(
        pod_plan(namespace="vigil-experiment", name="rebuild-demo-def", uid="pod-uid-3")
    )
    _, decision, attempt2, _ = asyncio.run(
        run_restart(second, tmp_path, cleanup, namespace="vigil-experiment", approve=False)
    )

    assert decision.disposition is RiskDisposition.AUTO
    assert attempt2.status.value == "verified"


def test_in_flight_proposal_blocks_auto(tmp_path, cleanup):
    first = FakeRestartAdapter(pod_plan(namespace="vigil-experiment"))
    proposal, _, _, _ = asyncio.run(
        run_restart(first, tmp_path, cleanup, namespace="vigil-experiment", approve=False)
    )

    async def mark_executing():
        async with Store.open(DSN) as store:
            await store.set_proposal_status(proposal.id, ProposalStatus.EXECUTING)

    asyncio.run(mark_executing())
    second = FakeRestartAdapter(
        pod_plan(namespace="vigil-experiment", name="rebuild-demo-def", uid="pod-uid-3")
    )
    _, decision, attempt, _ = asyncio.run(
        run_restart(second, tmp_path, cleanup, namespace="vigil-experiment", approve=False)
    )

    assert decision.disposition is RiskDisposition.APPROVAL
    assert attempt is None


def test_pending_proposal_does_not_clear_the_stop_gate(tmp_path, cleanup):
    first = FakeRestartAdapter(pod_plan(namespace="vigil-experiment"), service_status=500)
    _, decision, _, _ = asyncio.run(
        run_restart(first, tmp_path, cleanup, namespace="vigil-experiment", approve=False)
    )
    assert decision.disposition is RiskDisposition.AUTO

    second = FakeRestartAdapter(
        pod_plan(namespace="vigil-experiment", name="rebuild-demo-def", uid="pod-uid-3")
    )
    _, decision2, _, _ = asyncio.run(
        run_restart(second, tmp_path, cleanup, namespace="vigil-experiment", approve=False)
    )
    assert decision2.disposition is RiskDisposition.APPROVAL

    third = FakeRestartAdapter(
        pod_plan(namespace="vigil-experiment", name="rebuild-demo-ghi", uid="pod-uid-4")
    )
    _, decision3, attempt3, _ = asyncio.run(
        run_restart(third, tmp_path, cleanup, namespace="vigil-experiment", approve=False)
    )

    assert decision3.disposition is RiskDisposition.APPROVAL
    assert attempt3 is None
    assert third.calls == []


def test_rejected_proposal_does_not_clear_the_stop_gate(tmp_path, cleanup):
    first = FakeRestartAdapter(pod_plan(namespace="vigil-experiment"), service_status=500)
    asyncio.run(run_restart(first, tmp_path, cleanup, namespace="vigil-experiment", approve=False))

    second = FakeRestartAdapter(
        pod_plan(namespace="vigil-experiment", name="rebuild-demo-def", uid="pod-uid-3")
    )
    proposal, _, _, _ = asyncio.run(
        run_restart(second, tmp_path, cleanup, namespace="vigil-experiment", approve=False)
    )

    async def reject():
        async with Store.open(DSN) as store:
            await store.transition_proposal(
                proposal.id,
                expected=ProposalStatus.PENDING_APPROVAL,
                target=ProposalStatus.REJECTED,
            )

    asyncio.run(reject())
    third = FakeRestartAdapter(
        pod_plan(namespace="vigil-experiment", name="rebuild-demo-ghi", uid="pod-uid-4")
    )
    _, decision, attempt, _ = asyncio.run(
        run_restart(third, tmp_path, cleanup, namespace="vigil-experiment", approve=False)
    )

    assert decision.disposition is RiskDisposition.APPROVAL
    assert attempt is None


def test_legacy_rollback_failure_blocks_by_target_uid(tmp_path, cleanup):
    """修复前的 rollout_undo 没有 workload 字段，只能用 target_uid 关联到部署。"""
    legacy_id = f"act-{uuid4().hex[:12]}"

    async def save_legacy():
        async with Store.open(DSN) as store:
            await store.migrate()
            proposal = make_proposal(
                id=legacy_id,
                status=ProposalStatus.FAILED,
                target=ActionTarget(
                    cluster="vigil-lab",
                    namespace="vigil-experiment",
                    kind="Deployment",
                    name="rebuild-demo",
                    uid="deploy-uid-1",
                    resource_version="1",
                ),
                parameters={},
                idempotency_key=f"legacy-{uuid4().hex[:8]}",
            )
            decision = RiskDecision(
                proposal_id=proposal.id,
                disposition=RiskDisposition.APPROVAL,
                policy_version="legacy",
                reasons=["历史提案"],
                requires_compensation=False,
            )
            await store.save_proposal(proposal, decision)

    asyncio.run(save_legacy())
    cleanup.append(legacy_id)

    adapter = FakeRestartAdapter(
        pod_plan(
            namespace="vigil-experiment",
            name="rebuild-demo-def",
            uid="pod-uid-3",
            workload_uid="deploy-uid-1",
        )
    )
    _, decision, attempt, _ = asyncio.run(
        run_restart(adapter, tmp_path, cleanup, namespace="vigil-experiment", approve=False)
    )

    assert decision.disposition is RiskDisposition.APPROVAL
    assert attempt is None


def test_audit_failure_does_not_hide_the_proposal_id(tmp_path, cleanup, monkeypatch):
    adapter = FakeRestartAdapter(pod_plan(namespace="vigil-experiment"), service_status=500)

    async def broken_audit(*args, **kwargs):
        raise ConnectionError("审计写入失败")

    monkeypatch.setattr(ActionExecutor, "_audit", broken_audit)

    with pytest.raises(AutoExecutionError) as info:
        asyncio.run(
            run_restart(adapter, tmp_path, cleanup, namespace="vigil-experiment", approve=False)
        )

    assert info.value.proposal_id


def test_authorized_auto_proposal_is_stopped_when_the_workload_failed_meanwhile(tmp_path, cleanup):
    first = FakeRestartAdapter(pod_plan(namespace="vigil-experiment"), service_status=500)
    asyncio.run(run_restart(first, tmp_path, cleanup, namespace="vigil-experiment", approve=False))

    adapter = FakeRestartAdapter(
        pod_plan(namespace="vigil-experiment", name="rebuild-demo-def", uid="pod-uid-3")
    )
    proposal, decision, _, _ = asyncio.run(
        run_restart(adapter, tmp_path, cleanup, namespace="vigil-experiment", approve=False)
    )
    assert decision.disposition is RiskDisposition.APPROVAL

    async def authorize_and_resume():
        async with Store.open(DSN) as store:
            await store.migrate()
            executor = build_executor(
                store,
                FakeAdapter(rollout_plan()),
                FakePrometheus([1.0]),
                tmp_path,
                restart_adapter=adapter,
                verify_seconds=1,
                sample_seconds=0.2,
            )
            decided_at = datetime.now(UTC)
            approval = Approval(
                id=f"apr-{uuid4().hex[:12]}",
                proposal_id=proposal.id,
                proposal_hash=proposal_hash(proposal),
                actor="policy:test",
                decision=ApprovalDecision.APPROVED,
                reason="测试：执行前仍持有自动授权",
                decided_at=decided_at,
                expires_at=decided_at + timedelta(minutes=5),
            )
            await store.transition_proposal(
                proposal.id,
                expected=ProposalStatus.PENDING_APPROVAL,
                target=ProposalStatus.APPROVED,
            )
            await store.save_approval(approval)
            with pytest.raises(ActionError) as info:
                await executor.resume(proposal.id)
            return info.value.code

    assert asyncio.run(authorize_and_resume()) == "auto_stopped"
    assert adapter.calls == []


def test_restart_precheck_accepts_not_ready_endpoint(tmp_path, cleanup):
    adapter = FakeRestartAdapter(pod_plan(), service_selects=False, service_selects_not_ready=True)

    _, _, attempt, _ = asyncio.run(run_restart(adapter, tmp_path, cleanup))

    # 删除前选择关系成立（NotReady），删除后仍不出现就绪端点，所以最终失败但确实执行过。
    assert [call[0] for call in adapter.calls] == ["dry_run", "execute", "wait"]
    assert attempt.verification["service_selects_new_pod"] is False


def test_auto_execution_transport_error_keeps_the_proposal(tmp_path, cleanup):
    adapter = FakeRestartAdapter(
        pod_plan(namespace="vigil-experiment"), replan_error=ConnectionError("连接中断")
    )

    with pytest.raises(AutoExecutionError) as info:
        asyncio.run(
            run_restart(adapter, tmp_path, cleanup, namespace="vigil-experiment", approve=False)
        )

    cleanup.append(info.value.proposal_id)
    assert info.value.code == "auto_execute_error"


def test_restart_pod_is_proposed_then_verified(tmp_path, cleanup):
    adapter = FakeRestartAdapter(pod_plan())

    proposal, decision, attempt, saved = asyncio.run(run_restart(adapter, tmp_path, cleanup))

    assert decision.disposition is RiskDisposition.APPROVAL
    assert proposal.target.kind == "Pod"
    assert proposal.parameters["replica_set"] == "rebuild-demo-5f6"
    assert attempt.status.value == "verified"
    assert saved.status is ProposalStatus.SUCCEEDED
    assert attempt.verification["old_uid"] == "pod-uid-1"
    assert attempt.verification["new_uid"] == "pod-uid-2"
    assert attempt.verification["template_unchanged"] is True
    assert [call[0] for call in adapter.calls] == ["dry_run", "execute", "wait"]


def test_restart_with_changed_template_is_not_success(tmp_path, cleanup):
    adapter = FakeRestartAdapter(pod_plan(), rebuilt_spec_hash="spec-other")

    _, _, attempt, saved = asyncio.run(run_restart(adapter, tmp_path, cleanup))

    assert attempt.status.value == "failed"
    assert attempt.verification["template_unchanged"] is False
    assert saved.status is ProposalStatus.FAILED


def test_restart_stops_before_delete_when_service_does_not_select_pod(tmp_path, cleanup):
    adapter = FakeRestartAdapter(pod_plan(), service_selects=False)

    _, _, attempt, saved = asyncio.run(run_restart(adapter, tmp_path, cleanup))

    assert attempt is None
    assert saved.status is ProposalStatus.FAILED
    assert adapter.calls == []


def test_restart_with_service_that_stops_selecting_new_pod_is_not_success(tmp_path, cleanup):
    adapter = FakeRestartAdapter(pod_plan(), service_selects=True, service_selects_after=False)

    _, _, attempt, saved = asyncio.run(run_restart(adapter, tmp_path, cleanup))

    assert attempt.status.value == "failed"
    assert attempt.verification["service_selects_new_pod"] is False
    assert saved.status is ProposalStatus.FAILED


def test_restart_business_failure_is_not_success(tmp_path, cleanup):
    adapter = FakeRestartAdapter(pod_plan(), service_status=500)

    _, _, attempt, saved = asyncio.run(run_restart(adapter, tmp_path, cleanup))

    assert attempt.status.value == "failed"
    assert attempt.verification["ok"] is False
    assert saved.status is ProposalStatus.FAILED


def test_restart_drift_blocks_execution(tmp_path, cleanup):
    adapter = FakeRestartAdapter(pod_plan(), replan=pod_plan(uid="pod-uid-9"))

    _, _, attempt, saved = asyncio.run(run_restart(adapter, tmp_path, cleanup))

    assert attempt is None
    assert saved.status is ProposalStatus.FAILED
    assert adapter.calls == []


def test_restart_unknown_delete_result_stops_without_replay(tmp_path, cleanup):
    adapter = FakeRestartAdapter(pod_plan(), fail_on_delete=True)

    _, _, attempt, saved = asyncio.run(run_restart(adapter, tmp_path, cleanup))

    assert attempt.status.value == "unknown"
    assert saved.status is ProposalStatus.UNKNOWN
    assert [call[0] for call in adapter.calls] == ["dry_run", "execute"]


def test_restart_rechecks_policy_before_delete(tmp_path, cleanup):
    adapter = FakeRestartAdapter(pod_plan(), replan=pod_plan(discardable=False))

    _, _, attempt, saved = asyncio.run(run_restart(adapter, tmp_path, cleanup))

    assert attempt is None
    assert saved.status is ProposalStatus.FAILED
    assert adapter.calls == []


def test_restart_requires_a_newly_created_pod(tmp_path, cleanup):
    adapter = FakeRestartAdapter(pod_plan(), rebuild_available=False)

    _, _, attempt, saved = asyncio.run(run_restart(adapter, tmp_path, cleanup))

    assert attempt.status.value == "failed"
    assert attempt.verification["new_uid"] is None
    assert saved.status is ProposalStatus.FAILED


def test_restart_detects_restart_during_observation_window(tmp_path, cleanup):
    adapter = FakeRestartAdapter(pod_plan(), live_ready=False, live_restarts=3)

    _, _, attempt, saved = asyncio.run(run_restart(adapter, tmp_path, cleanup))

    assert attempt.status.value == "failed"
    assert attempt.verification["pod_restarts"] == 3
    assert attempt.verification["pod_ready"] is False
    assert saved.status is ProposalStatus.FAILED


def test_delete_request_is_bound_to_the_approved_uid():
    options = PodRestartAdapter()._delete_options(pod_plan())

    assert options.preconditions.uid == "pod-uid-1"
    assert options.preconditions.resource_version is None


def test_wait_rebuilt_ignores_pods_that_existed_before_deletion(monkeypatch):
    adapter = PodRestartAdapter()
    plan = pod_plan(sibling_uids=("sibling-uid",))
    monkeypatch.setattr(
        adapter,
        "pods_of_owner",
        lambda namespace, owner_uid: [
            {"name": "old", "uid": "pod-uid-1", "ready": True, "restarts": 0},
            {"name": "sibling", "uid": "sibling-uid", "ready": True, "restarts": 0},
        ],
    )

    assert adapter.wait_rebuilt(plan, 0.3, sample_seconds=0.1) is None
