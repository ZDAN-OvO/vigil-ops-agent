"""Pod 历史适配器的只读前置条件与历史处置事务。"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from actions_support import DSN, TEST_REASON, make_proposal, pod_plan

from aiops.actions.restart_pod import PodRestartAdapter
from aiops.actions.rollout_undo import ActionError
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


@pytest.mark.parametrize(
    ("actual_uid", "allowed", "code"),
    [
        ("pod-uid-1", True, None),
        ("another-pod", True, "precondition_failed"),
        ("pod-uid-1", False, "forbidden"),
    ],
)
def test_restart_dry_run_checks_uid_and_permission_without_delete(
    monkeypatch, actual_uid, allowed, code
):
    adapter = PodRestartAdapter()
    calls: list[str] = []

    class Core:
        def read_namespaced_pod(self, name, namespace):
            return SimpleNamespace(metadata=SimpleNamespace(uid=actual_uid))

        def delete_namespaced_pod(self, *args, **kwargs):
            calls.append("delete")

    class Auth:
        def __init__(self, client):
            pass

        def create_self_subject_access_review(self, review):
            return SimpleNamespace(status=SimpleNamespace(allowed=allowed))

    adapter._loaded = True
    adapter._core = Core()
    adapter._client = object()
    monkeypatch.setattr("aiops.actions.restart_pod.k8s_client.AuthorizationV1Api", Auth)

    if code is None:
        assert adapter.dry_run(pod_plan()) == {"dry_run": True, "uid": "pod-uid-1", "allowed": True}
    else:
        with pytest.raises(ActionError) as caught:
            adapter.dry_run(pod_plan())
        assert caught.value.code == code
    assert calls == []


@pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL")
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
