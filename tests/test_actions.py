"""受控回退：规则决定审批，执行前再校验，未知结果不重放。"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest

from aiops.actions.executor import ActionExecutor, proposal_hash
from aiops.actions.policy import decide
from aiops.actions.rollout_undo import ActionError, RolloutPlan
from aiops.config import Settings
from aiops.contracts import (
    ActionKind,
    ActionOrigin,
    ActionProposal,
    ActionTarget,
    ProposalStatus,
    RiskDisposition,
    TimeRange,
    ToolResult,
)
from aiops.db import Store

DSN = Settings().database_url
pytestmark = pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL，跳过真实库测试")

START = datetime.now(UTC)
WINDOW = TimeRange(start=START, end=START + timedelta(minutes=1))


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
    def __init__(self, plan, *, fail_on_apply=False):
        self._plan = plan
        self._fail_on_apply = fail_on_apply
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


def rollout_plan():
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
    )


@pytest.fixture
def cleanup():
    ids: list[str] = []
    yield ids
    with psycopg.connect(DSN) as conn:
        for proposal_id in ids:
            conn.execute("DELETE FROM action_locks WHERE proposal_id = %s", (proposal_id,))
            conn.execute("DELETE FROM action_attempts WHERE proposal_id = %s", (proposal_id,))
            conn.execute("DELETE FROM approvals WHERE proposal_id = %s", (proposal_id,))
            conn.execute("DELETE FROM audit_events WHERE correlation_id = %s", (proposal_id,))
            conn.execute("DELETE FROM action_proposals WHERE id = %s", (proposal_id,))


def build_executor(store, adapter, prometheus, tmp_path, *, approval_ttl_seconds=900):
    return ActionExecutor(
        store=store,
        adapter=adapter,
        prometheus=prometheus,
        runtime_path=tmp_path,
        cluster="vigil-lab",
        namespace="vigil-shop",
        verify_seconds=60,
        sample_seconds=15,
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
