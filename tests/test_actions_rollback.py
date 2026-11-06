"""受控回退：执行前校验、未知结果不重放。"""

import asyncio

import psycopg
import pytest
from actions_support import (
    DSN,
    FakeAdapter,
    FakePrometheus,
    build_executor,
    propose_and_approve,
    read_followup,
    rollout_plan,
)

from aiops.actions.rollout_undo import ActionError, RolloutPlan
from aiops.contracts import (
    ProposalStatus,
    RiskDisposition,
)
from aiops.db import Store

pytestmark = pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL，跳过真实库测试")


def test_proposal_waits_for_approval_then_verifies_recovery(tmp_path, cleanup):
    adapter = FakeAdapter(rollout_plan())
    proposal, decision, attempt, saved, approval = asyncio.run(
        propose_and_approve(
            adapter, FakePrometheus([200.0, 200.0, 200.0, 200.0]), tmp_path, cleanup
        )
    )

    assert decision.disposition is RiskDisposition.APPROVAL
    assert proposal.status is ProposalStatus.PENDING_APPROVAL
    assert saved is not None and saved.status is ProposalStatus.SUCCEEDED
    assert approval is not None and approval.proposal_hash
    assert attempt.status.value == "verified"
    assert attempt.verification["ok"] is True
    assert attempt.verification["outcome"] == "improved"
    assert adapter.calls == [True, False]


def test_failed_business_verification_is_not_success(tmp_path, cleanup):
    adapter = FakeAdapter(rollout_plan())
    _, _, attempt, saved, _ = asyncio.run(
        propose_and_approve(
            adapter, FakePrometheus([[200.0, 200.0], [500.0, 500.0, 500.0]]), tmp_path, cleanup
        )
    )

    assert attempt.status.value == "failed"
    assert saved.status is ProposalStatus.FAILED
    assert attempt.verification["tail_failures"] == 2
    assert attempt.verification["outcome"] == "worsened"
    assert attempt.verification["baseline"]["failure_ratio"] == 0.0


def test_missing_post_data_cannot_count_as_recovery(tmp_path, cleanup):
    adapter = FakeAdapter(rollout_plan())
    _, _, attempt, saved, _ = asyncio.run(
        propose_and_approve(adapter, FakePrometheus([[200.0, 200.0], []]), tmp_path, cleanup)
    )

    assert attempt.status.value == "unknown"
    assert attempt.verification["reason"] == "insufficient_data"
    assert attempt.verification["outcome"] == "unknown"
    assert saved.status is ProposalStatus.UNKNOWN


def test_non_finite_baseline_stops_before_write(tmp_path, cleanup):
    adapter = FakeAdapter(rollout_plan())

    async def scenario():
        async with Store.open(DSN) as store:
            await store.migrate()
            executor = build_executor(
                store,
                adapter,
                FakePrometheus([[float("nan")] * 3, [200.0, 200.0]]),
                tmp_path,
            )
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
    assert error.code == "baseline_unavailable"
    assert saved.status is ProposalStatus.FAILED
    assert attempt is None
    assert adapter.calls == []


def test_rollout_gateway_status_baseline_stops_before_write(tmp_path, cleanup):
    adapter = FakeAdapter(rollout_plan())

    async def scenario():
        async with Store.open(DSN) as store:
            await store.migrate()
            executor = build_executor(
                store,
                adapter,
                FakePrometheus([[503, 503, 503], [200, 200]]),
                tmp_path,
            )
            proposal, _ = await executor.propose_rollback(
                deployment="productcatalogservice", reason="坏发布", actor="tester"
            )
            cleanup.append(proposal.id)
            with pytest.raises(ActionError) as caught:
                await executor.approve(proposal.id, actor="tester")
            attempt = await store.get_attempt(proposal.id)
        return caught.value, attempt

    error, attempt = asyncio.run(scenario())
    assert error.code == "baseline_unavailable"
    assert attempt is None
    assert adapter.calls == []


def test_rollout_expiry_during_intent_write_stops_before_apply(monkeypatch, tmp_path, cleanup):
    original = Store.save_attempt

    async def slow_save(self, attempt):
        await asyncio.sleep(1.1)
        return await original(self, attempt)

    monkeypatch.setattr(Store, "save_attempt", slow_save)
    adapter = FakeAdapter(rollout_plan())

    async def scenario():
        async with Store.open(DSN) as store:
            await store.migrate()
            executor = build_executor(
                store,
                adapter,
                FakePrometheus([200.0, 200.0]),
                tmp_path,
                approval_ttl_seconds=1,
            )
            proposal, _ = await executor.propose_rollback(
                deployment="productcatalogservice", reason="坏发布", actor="tester"
            )
            cleanup.append(proposal.id)
            await executor.approve(proposal.id, actor="tester")
            saved = await store.get_proposal(proposal.id)
            attempt = await store.get_attempt(proposal.id)
        return saved, attempt

    saved, attempt = asyncio.run(scenario())
    assert saved.status is ProposalStatus.EXPIRED
    assert attempt is not None
    assert attempt.status.value == "failed"
    assert adapter.calls == [True]
    assert read_followup(attempt.id) is None


def test_rollout_wait_exception_is_unknown(tmp_path, cleanup):
    class BrokenWait(FakeAdapter):
        def wait_rolled_out(self, plan, timeout_seconds):
            raise ConnectionError("lost")

    adapter = BrokenWait(rollout_plan())
    _, _, attempt, saved, _ = asyncio.run(
        propose_and_approve(adapter, FakePrometheus([200.0, 200.0]), tmp_path, cleanup)
    )

    assert attempt.status.value == "unknown"
    assert attempt.verification["outcome"] == "unknown"
    assert attempt.finished_at is not None
    assert saved.status is ProposalStatus.UNKNOWN


def test_rollout_tail_with_non_finite_values_is_unknown(tmp_path, cleanup):
    adapter = FakeAdapter(rollout_plan())
    _, _, attempt, saved, _ = asyncio.run(
        propose_and_approve(
            adapter,
            FakePrometheus([[200.0, 200.0, 200.0], [200.0, 200.0, float("nan"), float("inf")]]),
            tmp_path,
            cleanup,
        )
    )

    assert attempt.status.value == "unknown"
    assert attempt.verification["outcome"] == "unknown"
    assert attempt.verification["tail_unavailable_samples"] >= 1
    assert saved.status is ProposalStatus.UNKNOWN
    followup = read_followup(attempt.id)
    assert followup is not None
    assert followup.outcome.value == "unknown"


def test_rollout_resource_failure_with_missing_post_data_is_failed(tmp_path, cleanup):
    class NotRolledOut(FakeAdapter):
        def wait_rolled_out(self, plan, timeout_seconds):
            return False

    adapter = NotRolledOut(rollout_plan())
    _, _, attempt, saved, _ = asyncio.run(
        propose_and_approve(adapter, FakePrometheus([[200.0, 200.0], []]), tmp_path, cleanup)
    )

    assert attempt.status.value == "failed"
    assert attempt.verification["outcome"] == "unknown"
    assert saved.status is ProposalStatus.FAILED


def test_missing_baseline_stops_before_write(tmp_path, cleanup):
    adapter = FakeAdapter(rollout_plan())

    async def scenario():
        async with Store.open(DSN) as store:
            await store.migrate()
            executor = build_executor(
                store, adapter, FakePrometheus([[], [200.0, 200.0]]), tmp_path
            )
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
    assert error.code == "baseline_unavailable"
    assert saved.status is ProposalStatus.FAILED
    assert attempt is None
    assert adapter.calls == []


def test_unknown_apply_result_stops_without_replay(tmp_path, cleanup):
    adapter = FakeAdapter(rollout_plan(), fail_on_apply=True)
    _, _, attempt, saved, _ = asyncio.run(
        propose_and_approve(adapter, FakePrometheus([200.0, 200.0]), tmp_path, cleanup)
    )

    assert attempt.status.value == "unknown"
    assert saved.status is ProposalStatus.UNKNOWN
    assert adapter.calls == [True, False]
    assert attempt.verification["outcome"] == "unknown"
    assert attempt.verification["baseline"]["samples"] > 0


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
            executor = build_executor(store, adapter, FakePrometheus([200.0, 200.0]), tmp_path)
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
            executor = build_executor(store, adapter, FakePrometheus([200.0, 200.0]), tmp_path)
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
            executor = build_executor(store, adapter, FakePrometheus([200.0, 200.0]), tmp_path)
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
            executor = build_executor(store, adapter, FakePrometheus([200.0, 200.0]), tmp_path)
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
                store, adapter, FakePrometheus([200.0, 200.0]), tmp_path, approval_ttl_seconds=0
            )
            proposal, _ = await executor.propose_rollback(
                deployment="productcatalogservice", reason="坏发布", actor="tester"
            )
            cleanup.append(proposal.id)
            await executor.approve(proposal.id, actor="tester", defer=True)
        async with Store.open(DSN) as store:
            executor = build_executor(
                store, adapter, FakePrometheus([200.0, 200.0]), tmp_path, approval_ttl_seconds=0
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
            executor = build_executor(store, adapter, FakePrometheus([200.0, 200.0]), tmp_path)
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
            executor = build_executor(store, adapter, FakePrometheus([200.0, 200.0]), tmp_path)
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
            executor = build_executor(store, adapter, FakePrometheus([200.0, 200.0]), tmp_path)
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
            executor = build_executor(store, adapter, FakePrometheus([200.0, 200.0]), tmp_path)
            proposal, _ = await executor.propose_rollback(
                deployment="productcatalogservice", reason="坏发布", actor="tester"
            )
            cleanup.append(proposal.id)
        return proposal.id

    async def second_process(proposal_id):
        async with Store.open(DSN) as store:
            await store.migrate()
            executor = build_executor(store, adapter, FakePrometheus([200.0, 200.0]), tmp_path)
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
            executor_a = build_executor(store_a, adapter, FakePrometheus([200.0, 200.0]), tmp_path)
            proposal, _ = await executor_a.propose_rollback(
                deployment="productcatalogservice", reason="坏发布", actor="tester"
            )
            cleanup.append(proposal.id)
            executor_b = build_executor(store_b, adapter, FakePrometheus([200.0, 200.0]), tmp_path)
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
        propose_and_approve(
            adapter, FakePrometheus([200.0, 200.0, 200.0, 200.0]), tmp_path, cleanup
        )
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
        propose_and_approve(
            adapter, FakePrometheus([200.0, 200.0, 200.0, 200.0]), tmp_path, cleanup
        )
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
        propose_and_approve(
            adapter, FakePrometheus([200.0, 200.0, 200.0, 200.0]), tmp_path, cleanup
        )
    )

    assert attempt.status.value == "failed"
    assert attempt.verification["pod_restarts"] == 2
    assert saved.status is ProposalStatus.FAILED


def test_proposal_binds_memory_limits(tmp_path, cleanup):
    adapter = FakeAdapter(rollout_plan(current_memory_limit="16Mi", target_memory_limit="128Mi"))

    async def scenario():
        async with Store.open(DSN) as store:
            await store.migrate()
            executor = build_executor(store, adapter, FakePrometheus([200.0, 200.0]), tmp_path)
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
