"""自动重建：停止门、复查与跨集群边界。"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from actions_support import (
    DSN,
    TEST_REASON,
    FakeAdapter,
    FakePrometheus,
    FakeRestartAdapter,
    build_executor,
    make_proposal,
    pod_plan,
    rollout_plan,
    run_restart,
)

from aiops.actions.executor import (
    ActionExecutor,
    AutoExecutionError,
    proposal_hash,
)
from aiops.actions.restart_pod import PodRestartAdapter
from aiops.actions.rollout_undo import ActionError
from aiops.contracts import (
    ActionOrigin,
    ActionTarget,
    Approval,
    ApprovalDecision,
    ProposalStatus,
    RiskDecision,
    RiskDisposition,
)
from aiops.db import Store

pytestmark = pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL，跳过真实库测试")


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
                FakePrometheus([200.0]),
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
                FakePrometheus([200.0]),
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
    assert attempt.verification["outcome"] == "unknown"
    assert "资源检查" in attempt.verification["outcome_reason"]
    assert saved.status is ProposalStatus.FAILED


def test_restart_stops_before_delete_when_service_does_not_select_pod(tmp_path, cleanup):
    adapter = FakeRestartAdapter(pod_plan(), service_selects=False)

    _, _, attempt, saved = asyncio.run(run_restart(adapter, tmp_path, cleanup))

    assert attempt is None
    assert saved.status is ProposalStatus.FAILED
    assert adapter.calls == []


def test_restart_with_service_that_stops_selecting_new_pod_is_not_success(tmp_path, cleanup):
    adapter = FakeRestartAdapter(
        pod_plan(),
        service_selects=True,
        service_selects_after=False,
        service_selects_after_call=3,
    )

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
