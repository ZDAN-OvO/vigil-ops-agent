"""Pod 重建：删除前复查、业务验证与失败停止。"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
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
    read_followup,
    rollout_plan,
    run_restart,
)

from aiops.actions.executor import (
    AutoExecutionError,
)
from aiops.actions.restart_pod import PodRestartAdapter
from aiops.contracts import (
    ActionAttempt,
    ActionFollowup,
    AttemptOutcome,
    AttemptStatus,
    ProposalStatus,
    RiskDecision,
    RiskDisposition,
)
from aiops.db import Store

pytestmark = pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL，跳过真实库测试")


def test_restart_dry_run_does_not_delete_the_pod(monkeypatch):
    adapter = PodRestartAdapter()
    calls: list[str] = []

    class Core:
        def read_namespaced_pod(self, name, namespace):
            return SimpleNamespace(metadata=SimpleNamespace(uid="pod-uid-1"))

        def delete_namespaced_pod(self, *args, **kwargs):
            calls.append("delete")

    class Auth:
        def __init__(self, client):
            pass

        def create_self_subject_access_review(self, review):
            return SimpleNamespace(status=SimpleNamespace(allowed=True))

    adapter._loaded = True
    adapter._core = Core()
    adapter._client = object()
    monkeypatch.setattr("aiops.actions.restart_pod.k8s_client.AuthorizationV1Api", Auth)

    result = adapter.dry_run(pod_plan())

    assert result == {"dry_run": True, "uid": "pod-uid-1", "allowed": True}
    assert calls == []


def test_finish_attempt_rolls_back_when_followup_insert_fails(monkeypatch, cleanup):
    suffix = uuid4().hex[:8]
    proposal = make_proposal(
        id=f"act-atomic-{suffix}",
        idempotency_key=f"atomic-{suffix}",
        reason=TEST_REASON,
    )
    attempt = ActionAttempt(
        id=f"att-atomic-{suffix}",
        proposal_id=proposal.id,
        status=AttemptStatus.EXECUTING,
        idempotency_key=proposal.idempotency_key,
        snapshot={},
        request={},
        started_at=datetime.now(UTC),
    )
    followup = ActionFollowup(
        id=f"fol-atomic-{suffix}",
        proposal_id=proposal.id,
        attempt_id=attempt.id,
        cluster="vigil-lab",
        namespace="vigil-shop",
        target_kind="Deployment",
        target_name="productcatalogservice",
        outcome=AttemptOutcome.WORSENED,
        reason="测试",
        evidence={},
        created_at=datetime.now(UTC),
    )

    async def boom(self, conn, item):
        raise RuntimeError("forced")

    monkeypatch.setattr(Store, "_insert_followup", boom)

    async def scenario():
        async with Store.open(DSN) as store:
            await store.migrate()
            decision = RiskDecision(
                proposal_id=proposal.id,
                disposition=RiskDisposition.APPROVAL,
                policy_version="test",
                reasons=["测试"],
                requires_compensation=False,
            )
            await store.save_proposal(proposal, decision)
            cleanup.append(proposal.id)
            await store.save_attempt(attempt)
            with pytest.raises(RuntimeError):
                await store.finish_attempt(
                    attempt.id,
                    proposal_id=proposal.id,
                    proposal_status=ProposalStatus.FAILED,
                    status=AttemptStatus.FAILED,
                    result={"stage": "test"},
                    verification={"outcome": "worsened"},
                    finished_at=datetime.now(UTC),
                    followup=followup,
                )
            saved_attempt = await store.get_attempt(proposal.id)
            saved_proposal = await store.get_proposal(proposal.id)
        return saved_attempt, saved_proposal

    saved_attempt, saved_proposal = asyncio.run(scenario())
    assert saved_attempt is not None and saved_attempt.status is AttemptStatus.EXECUTING
    assert saved_proposal is not None
    assert saved_proposal.status is ProposalStatus.PENDING_APPROVAL


def test_restart_worsened_when_baseline_was_healthy(tmp_path, cleanup):
    adapter = FakeRestartAdapter(
        pod_plan(namespace="vigil-experiment"), statuses=[200, 200] + [500] * 8
    )

    _, _, attempt, saved = asyncio.run(
        run_restart(adapter, tmp_path, cleanup, namespace="vigil-experiment", approve=False)
    )

    assert attempt.status.value == "failed"
    assert attempt.verification["outcome"] == "worsened"
    assert attempt.verification["baseline"]["failure_ratio"] == 0.0
    assert saved.status is ProposalStatus.FAILED
    followup = read_followup(attempt.id)
    assert followup is not None
    assert followup.outcome.value == "worsened"
    assert followup.status.value == "open"
    assert followup.evidence["outcome"] == "worsened"

    async def resolve_and_audit():
        async with Store.open(DSN) as store:
            resolved = await store.resolve_followup(followup.id, actor="tester", note="已核对")
            events = await store.list_audit(attempt.proposal_id)
        return resolved, events

    resolved, events = asyncio.run(resolve_and_audit())
    assert resolved is not None and resolved.status.value == "resolved"
    assert any(event.event == "action.followup_resolved" for event in events)


def test_restart_no_improvement_when_baseline_already_failing(tmp_path, cleanup):
    adapter = FakeRestartAdapter(pod_plan(namespace="vigil-experiment"), statuses=[500] * 10)

    _, _, attempt, saved = asyncio.run(
        run_restart(adapter, tmp_path, cleanup, namespace="vigil-experiment", approve=False)
    )

    assert attempt.status.value == "failed"
    assert attempt.verification["outcome"] == "no_improvement"
    assert attempt.verification["baseline"]["failure_ratio"] == 1.0
    assert saved.status is ProposalStatus.FAILED
    followup = read_followup(attempt.id)
    assert followup is not None
    assert followup.outcome.value == "no_improvement"


def test_restart_rechecks_discardable_after_baseline(tmp_path, cleanup):
    plan = pod_plan(namespace="vigil-experiment")
    adapter = FakeRestartAdapter(plan, replan=replace(plan, discardable=False), replan_after=2)

    async def scenario():
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
            with pytest.raises(AutoExecutionError) as caught:
                await executor.propose_restart_pod(
                    pod="rebuild-demo-abc",
                    service="rebuild-demo",
                    path="/work",
                    reason=TEST_REASON,
                    actor="tester",
                    namespace="vigil-experiment",
                )
            cleanup.append(caught.value.proposal_id)
            saved = await store.get_proposal(caught.value.proposal_id)
            attempt = await store.get_attempt(caught.value.proposal_id)
        return caught.value, saved, attempt

    error, saved, attempt = asyncio.run(scenario())
    assert error.code == "policy_denied"
    assert saved.status is ProposalStatus.FAILED
    assert attempt is None
    assert adapter.pod_get_calls >= 2
    assert [call[0] for call in adapter.calls] == []


def test_restart_rechecks_discardable_after_dry_run(tmp_path, cleanup):
    plan = pod_plan(namespace="vigil-experiment")
    adapter = FakeRestartAdapter(plan, replan=replace(plan, discardable=False), replan_after=3)

    async def scenario():
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
            with pytest.raises(AutoExecutionError) as caught:
                await executor.propose_restart_pod(
                    pod="rebuild-demo-abc",
                    service="rebuild-demo",
                    path="/work",
                    reason=TEST_REASON,
                    actor="tester",
                    namespace="vigil-experiment",
                )
            cleanup.append(caught.value.proposal_id)
            saved = await store.get_proposal(caught.value.proposal_id)
            attempt = await store.get_attempt(caught.value.proposal_id)
        return caught.value, saved, attempt

    error, saved, attempt = asyncio.run(scenario())
    assert error.code == "policy_denied"
    assert saved.status is ProposalStatus.FAILED
    assert attempt is None
    assert [call[0] for call in adapter.calls] == ["dry_run"]
    assert adapter.pod_get_calls >= 2


def test_restart_expiry_during_baseline_stops_before_delete(tmp_path, cleanup):
    adapter = FakeRestartAdapter(pod_plan(namespace="vigil-experiment"))

    async def scenario():
        async with Store.open(DSN) as store:
            await store.migrate()
            executor = build_executor(
                store,
                FakeAdapter(rollout_plan()),
                FakePrometheus([200.0]),
                tmp_path,
                restart_adapter=adapter,
                verify_seconds=3,
                sample_seconds=1.2,
                approval_ttl_seconds=1,
            )
            with pytest.raises(AutoExecutionError) as caught:
                await executor.propose_restart_pod(
                    pod="rebuild-demo-abc",
                    service="rebuild-demo",
                    path="/work",
                    reason=TEST_REASON,
                    actor="tester",
                    namespace="vigil-experiment",
                )
            cleanup.append(caught.value.proposal_id)
            saved = await store.get_proposal(caught.value.proposal_id)
            attempt = await store.get_attempt(caught.value.proposal_id)
        return caught.value, saved, attempt

    error, saved, attempt = asyncio.run(scenario())
    assert error.code == "expired"
    assert saved.status is ProposalStatus.EXPIRED
    assert attempt is None
    assert [call[0] for call in adapter.calls] == []


def test_restart_expiry_during_intent_write_stops_before_delete(monkeypatch, tmp_path, cleanup):
    original = Store.save_attempt

    async def slow_save(self, attempt):
        await asyncio.sleep(1.1)
        return await original(self, attempt)

    monkeypatch.setattr(Store, "save_attempt", slow_save)
    adapter = FakeRestartAdapter(pod_plan(namespace="vigil-experiment"))

    async def scenario():
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
                approval_ttl_seconds=1,
            )
            proposal, _ = await executor.propose_restart_pod(
                pod="rebuild-demo-abc",
                service="rebuild-demo",
                path="/work",
                reason=TEST_REASON,
                actor="tester",
                namespace="vigil-experiment",
            )
            cleanup.append(proposal.id)
            saved = await store.get_proposal(proposal.id)
            attempt = await store.get_attempt(proposal.id)
        return saved, attempt

    saved, attempt = asyncio.run(scenario())
    assert saved.status is ProposalStatus.EXPIRED
    assert attempt is not None
    assert attempt.status.value == "failed"
    assert [call[0] for call in adapter.calls] == ["dry_run"]
    assert read_followup(attempt.id) is None


def test_restart_proxy_error_is_not_a_business_baseline(tmp_path, cleanup):
    adapter = FakeRestartAdapter(pod_plan(namespace="vigil-experiment"), statuses=[403, 403])

    async def scenario():
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
            with pytest.raises(AutoExecutionError) as caught:
                await executor.propose_restart_pod(
                    pod="rebuild-demo-abc",
                    service="rebuild-demo",
                    path="/work",
                    reason=TEST_REASON,
                    actor="tester",
                    namespace="vigil-experiment",
                )
            cleanup.append(caught.value.proposal_id)
            attempt = await store.get_attempt(caught.value.proposal_id)
        return caught.value, attempt

    error, attempt = asyncio.run(scenario())
    assert error.code == "baseline_unavailable"
    assert attempt is None
    assert [call[0] for call in adapter.calls] == []


def test_restart_ignores_warmup_failures_outside_the_tail(tmp_path, cleanup):
    adapter = FakeRestartAdapter(
        pod_plan(namespace="vigil-experiment"),
        statuses=[200, 200, 500, 200, 200, 200, 200],
    )

    _, _, attempt, saved = asyncio.run(
        run_restart(adapter, tmp_path, cleanup, namespace="vigil-experiment", approve=False)
    )

    assert attempt.status.value == "verified"
    assert attempt.verification["outcome"] == "improved"
    assert attempt.verification["post"]["failed_samples"] == 1
    assert attempt.verification["post_tail"]["failed_samples"] == 0
    assert saved.status is ProposalStatus.SUCCEEDED
    assert read_followup(attempt.id) is None


def test_restart_without_post_business_responses_is_unknown(tmp_path, cleanup):
    adapter = FakeRestartAdapter(
        pod_plan(namespace="vigil-experiment"), statuses=[200, 200, None, None, None]
    )

    _, _, attempt, saved = asyncio.run(
        run_restart(adapter, tmp_path, cleanup, namespace="vigil-experiment", approve=False)
    )

    assert attempt.status.value == "unknown"
    assert attempt.verification["outcome"] == "unknown"
    assert attempt.verification["unavailable_samples"] >= 1
    assert saved.status is ProposalStatus.UNKNOWN


def test_restart_tail_without_business_responses_is_unknown(tmp_path, cleanup):
    adapter = FakeRestartAdapter(
        pod_plan(namespace="vigil-experiment"), statuses=[200, 200] + [503] * 6
    )

    _, _, attempt, saved = asyncio.run(
        run_restart(adapter, tmp_path, cleanup, namespace="vigil-experiment", approve=False)
    )

    assert attempt.status.value == "unknown"
    assert attempt.verification["outcome"] == "unknown"
    assert attempt.verification["tail_unavailable_samples"] >= 1
    assert saved.status is ProposalStatus.UNKNOWN
    followup = read_followup(attempt.id)
    assert followup is not None
    assert followup.outcome.value == "unknown"


def test_restart_dry_run_failure_creates_no_attempt(tmp_path, cleanup):
    adapter = FakeRestartAdapter(pod_plan(namespace="vigil-experiment"), fail_on_dry_run=True)

    async def scenario():
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
            with pytest.raises(AutoExecutionError) as caught:
                await executor.propose_restart_pod(
                    pod="rebuild-demo-abc",
                    service="rebuild-demo",
                    path="/work",
                    reason=TEST_REASON,
                    actor="tester",
                    namespace="vigil-experiment",
                )
            cleanup.append(caught.value.proposal_id)
            saved = await store.get_proposal(caught.value.proposal_id)
            attempt = await store.get_attempt(caught.value.proposal_id)
        return caught.value, saved, attempt

    error, saved, attempt = asyncio.run(scenario())
    assert error.code == "precondition_failed"
    assert saved.status is ProposalStatus.FAILED
    assert attempt is None
    assert [call[0] for call in adapter.calls] == ["dry_run"]


def test_restart_missing_baseline_stops_before_delete(tmp_path, cleanup):
    adapter = FakeRestartAdapter(pod_plan(namespace="vigil-experiment"), statuses=[None, None])

    async def scenario():
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
            with pytest.raises(AutoExecutionError) as caught:
                await executor.propose_restart_pod(
                    pod="rebuild-demo-abc",
                    service="rebuild-demo",
                    path="/work",
                    reason=TEST_REASON,
                    actor="tester",
                    namespace="vigil-experiment",
                )
            proposal_id = caught.value.proposal_id
            cleanup.append(proposal_id)
            saved = await store.get_proposal(proposal_id)
            attempt = await store.get_attempt(proposal_id)
        return caught.value, saved, attempt

    error, saved, attempt = asyncio.run(scenario())
    assert error.code == "baseline_unavailable"
    assert saved.status is ProposalStatus.FAILED
    assert attempt is None
    assert [call[0] for call in adapter.calls] == []
