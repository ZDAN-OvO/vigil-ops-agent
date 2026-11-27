"""旧回退写入切换与受控动作的审批、接手回归。"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from actions_support import (
    DSN,
    FakeAdapter,
    FakePrometheus,
    build_executor,
    rollout_plan,
    seed_legacy_approval,
    seed_legacy_attempt,
    seed_legacy_card,
)
from controlled_action_support import drive_replica, intent_for, prepare_replica, replica_runtime

from aiops.actions.rollout_undo import ActionError, RolloutUndoAdapter
from aiops.actions.safety import ActionSafety
from aiops.contracts import (
    ActionOrigin,
    ApprovalDecision,
    AttemptStatus,
    ProposalStatus,
    RiskDecision,
    RiskDisposition,
)
from aiops.db import DuplicateProposal, Store
from aiops.sessions.projection import project_action_result

requires_db = pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL")


@requires_db
@pytest.mark.parametrize("origin", [ActionOrigin.OPERATOR, ActionOrigin.MODEL])
def test_full_template_rollback_is_denied_without_approval_or_write(tmp_path, cleanup, origin):
    async def run():
        adapter = FakeAdapter(
            rollout_plan(current_memory_limit="16Mi", target_memory_limit="128Mi")
        )
        async with Store.open(DSN) as store:
            await store.migrate()
            executor = await build_executor(
                store, adapter, FakePrometheus([200.0]), tmp_path, cleanup=cleanup
            )
            proposal, risk = await executor.propose_rollback(
                deployment="productcatalogservice",
                reason="内存限额不足",
                actor="tester",
                origin=origin,
                evidence_ids=["ev-1"] if origin is ActionOrigin.MODEL else [],
            )
            cleanup.append(proposal.id)
            saved = await store.get_proposal(proposal.id)
            assert risk.disposition is RiskDisposition.DENY
            assert saved.status is ProposalStatus.DENIED
            assert saved.parameters["from_memory_limit"] == "16Mi"
            assert saved.parameters["to_memory_limit"] == "128Mi"
            assert await store.get_approval(proposal.id) is None
            assert await store.get_attempt(proposal.id) is None
            assert (
                await executor._safety.get_card(proposal.id, session_id=proposal.session_id) is None
            )
            assert adapter.calls == []

    asyncio.run(run())


def test_rollback_adapter_dry_run_still_binds_uid_and_version(monkeypatch):
    requests = []
    adapter = RolloutUndoAdapter()
    monkeypatch.setattr(
        adapter,
        "_api",
        lambda: SimpleNamespace(
            patch_namespaced_deployment=lambda *args, **kw: requests.append((args, kw))
        ),
    )
    plan = rollout_plan()

    assert adapter.apply(plan, dry_run=True, proposal_id="legacy")["dry_run"] is True

    (((name, namespace, patch), options),) = requests
    assert (name, namespace) == (plan.name, plan.namespace)
    assert options["dry_run"] == "All"
    assert patch[:3] == [
        {"op": "test", "path": "/metadata/uid", "value": plan.deployment_uid},
        {"op": "test", "path": "/metadata/resourceVersion", "value": plan.resource_version},
        {"op": "replace", "path": "/spec/template", "value": plan.template},
    ]


@requires_db
@pytest.mark.parametrize(
    "status,proposal_status,stage,written",
    [
        (AttemptStatus.VERIFIED, ProposalStatus.SUCCEEDED, "applied", True),
        (AttemptStatus.FAILED, ProposalStatus.FAILED, "applied", True),
        (AttemptStatus.UNKNOWN, ProposalStatus.UNKNOWN, "apply", None),
    ],
)
def test_historical_rollback_receipt_can_be_read_without_replay(
    tmp_path, cleanup, status, proposal_status, stage, written
):
    async def run():
        adapter = FakeAdapter(rollout_plan())
        async with Store.open(DSN) as store:
            await store.migrate()
            executor = await build_executor(
                store, adapter, FakePrometheus([]), tmp_path, cleanup=cleanup
            )
            proposal, card = await seed_legacy_card(store, executor, cleanup)
            approval = await seed_legacy_approval(store, proposal)
            outcome = {
                AttemptStatus.VERIFIED: "improved",
                AttemptStatus.FAILED: "no_improvement",
                AttemptStatus.UNKNOWN: "unknown",
            }[status]
            receipt = await seed_legacy_attempt(
                store,
                proposal,
                status=status,
                snapshot={"approval_hash": approval.proposal_hash},
                result={"stage": stage},
                verification={"outcome": outcome, "memory_limit_restored": written is True},
            )
            await store.set_proposal_status(proposal.id, proposal_status)
            safety = ActionSafety(store)
            displayed = await safety.get_card(proposal.id, session_id=proposal.session_id)
            assert displayed["fingerprint"] == card.payload["fingerprint"] == approval.proposal_hash
            assert displayed["action"] == "rollout_undo"
            assert displayed["status"] == proposal_status
            assert (
                await safety.get_card_decision(proposal.id, session_id=proposal.session_id)
                == approval
            )
            assert await executor.read_action_attempt(proposal.id) == receipt
            assert project_action_result(receipt)["written"] is written
            assert receipt.verification["outcome"] == outcome
            if status is AttemptStatus.VERIFIED:
                history = await safety.list_verified_rollbacks(
                    since=datetime.now(UTC) - timedelta(minutes=1), cluster=proposal.target.cluster
                )
                entry = next(item for item in history if item["card_id"] == proposal.id)
                assert (entry["approved_by"], entry["target_revision"]) == ("tester", "1")
            assert adapter.calls == []

    asyncio.run(run())


async def _new_replica(store, tmp_path, cleanup):
    await store.migrate()
    executor = await build_executor(
        store, FakeAdapter(rollout_plan()), FakePrometheus([]), tmp_path, cleanup=cleanup
    )
    runtime = replica_runtime(store)
    proposal = await prepare_replica(executor, runtime)
    cleanup.append(proposal.id)
    card = await executor.publish_test_card(proposal.id)
    return executor, runtime, proposal, card


async def _decide(store, proposal, card, *, decision=ApprovalDecision.APPROVED, actor="tester"):
    return await ActionSafety(store).record_card_decision(
        proposal.id,
        session_id=proposal.session_id,
        fingerprint=card.payload["fingerprint"],
        decision=decision,
        actor=actor,
    )


@requires_db
@pytest.mark.parametrize("missing", ["baseline", "observation"])
def test_missing_business_samples_stop_or_prevent_success(tmp_path, cleanup, missing):
    async def run():
        async with Store.open(DSN) as store:
            _, runtime, proposal, card = await _new_replica(store, tmp_path, cleanup)
            await _decide(store, proposal, card)
            if missing == "baseline":
                runtime.metrics.before = None
            else:
                runtime.metrics.after = None
            receipt = await drive_replica(runtime, proposal.id)
            saved = await store.get_proposal(proposal.id)
            if missing == "baseline":
                assert receipt is None
                assert saved.status is ProposalStatus.FAILED
                assert runtime.kube.dry_runs == runtime.kube.writes == []
            else:
                assert receipt.verification["outcome"] == "unknown"
                assert receipt.verification["slots_collected"] == 0
                assert runtime.kube.writes == [{"replicas": 2}]
                assert saved.status is not ProposalStatus.SUCCEEDED
                (followup,) = await store.list_followups(proposal_id=proposal.id)
                assert followup.attempt_id == receipt.id

    asyncio.run(run())


@requires_db
def test_approved_action_survives_process_restart_and_sends_once(tmp_path, cleanup):
    async def run():
        async with Store.open(DSN) as first:
            _, runtime, proposal, card = await _new_replica(first, tmp_path, cleanup)
            assert await runtime.ledger.claim(proposal.id) is None
            assert await first.get_approval(proposal.id) is None
            approval = await _decide(first, proposal, card)
            assert runtime.kube.writes == []
        async with Store.open(DSN) as restarted:
            runtime = replica_runtime(restarted)
            receipt = await drive_replica(runtime, proposal.id)
            assert receipt.status is AttemptStatus.VERIFIED
            assert receipt.verification["outcome"] == "improved"
            assert (await restarted.get_proposal(proposal.id)).status is ProposalStatus.SUCCEEDED
            assert await restarted.get_approval(proposal.id) == approval
            assert await drive_replica(runtime, proposal.id) == receipt
            assert runtime.kube.writes == [{"replicas": 2}]
            with pytest.raises(ActionError) as caught:
                await _decide(restarted, proposal, card)
            assert caught.value.code == "not_pending"

    asyncio.run(run())


@requires_db
@pytest.mark.parametrize("change", ["expiry", "parameters"])
def test_invalidated_approval_cannot_grant_send(tmp_path, cleanup, change):
    async def run():
        async with Store.open(DSN) as store:
            _, runtime, proposal, card = await _new_replica(store, tmp_path, cleanup)
            approval = await _decide(store, proposal, card)
            claim = await runtime.ledger.claim(proposal.id)
            assert claim is not None
            async with store.transaction() as conn:
                if change == "expiry":
                    await conn.execute(
                        "UPDATE approvals SET expires_at = decided_at WHERE id = %s", (approval.id,)
                    )
                else:
                    await conn.execute(
                        "UPDATE action_proposals "
                        "SET parameters = jsonb_build_object('replicas', 3) "
                        "WHERE id = %s",
                        (proposal.id,),
                    )
            with pytest.raises(ActionError) as caught:
                await runtime.ledger.grant_send(
                    claim, intent_for(proposal), authorize=lambda *_: pytest.fail("批准已失效")
                )
            assert caught.value.code in {"no_approval", "expired", "hash_mismatch"}
            assert await store.get_attempt(proposal.id) is None
            assert runtime.kube.writes == []
            assert not any(
                event.event == "action.send_granted"
                for event in await store.list_audit(proposal.id)
            )

    asyncio.run(run())


@requires_db
def test_rejection_after_restart_never_touches_cluster(tmp_path, cleanup):
    async def run():
        async with Store.open(DSN) as first:
            _, _, proposal, card = await _new_replica(first, tmp_path, cleanup)
        async with Store.open(DSN) as restarted:
            rejected = await _decide(restarted, proposal, card, decision=ApprovalDecision.REJECTED)
            assert rejected.expires_at is None
            runtime = replica_runtime(restarted)
            assert await drive_replica(runtime, proposal.id) is None
            assert (await restarted.get_proposal(proposal.id)).status is ProposalStatus.REJECTED
            assert runtime.kube.dry_runs == runtime.kube.writes == []

    asyncio.run(run())


@requires_db
def test_concurrent_approvals_only_one_wins(tmp_path, cleanup):
    async def run():
        async with Store.open(DSN) as store_a, Store.open(DSN) as store_b:
            _, _, proposal, card = await _new_replica(store_a, tmp_path, cleanup)
            results = await asyncio.gather(
                _decide(store_a, proposal, card, actor="alice"),
                _decide(store_b, proposal, card, actor="bob"),
                return_exceptions=True,
            )
            saved = await store_a.get_approval(proposal.id)
            assert saved in results
            (error,) = [result for result in results if isinstance(result, ActionError)]
            assert error.code == "not_pending"
            async with store_a.transaction() as conn:
                cursor = await conn.execute(
                    "SELECT count(*) AS total FROM approvals WHERE proposal_id = %s", (proposal.id,)
                )
                assert (await cursor.fetchone())["total"] == 1

    asyncio.run(run())


@requires_db
def test_abandoned_proposal_releases_the_idempotency_key(tmp_path, cleanup):
    async def run():
        async with Store.open(DSN) as store:
            executor, runtime, first, _ = await _new_replica(store, tmp_path, cleanup)
            retry = first.model_copy(update={"id": f"act-{uuid4().hex}"})
            retry_risk = RiskDecision(
                proposal_id=retry.id,
                disposition=RiskDisposition.APPROVAL,
                policy_version=runtime.policy.version,
                reasons=["重试原提案"],
                requires_compensation=False,
            )
            lease, turn_id = executor._scope()
            cleanup.append(retry.id)
            with pytest.raises(DuplicateProposal):
                await runtime.ledger.save_manual(
                    retry, retry_risk, {}, lease=lease, turn_id=turn_id
                )
            revoked = await executor.abandon(first.id, actor="budget-guard", reason="预算不足")
            assert revoked.status is ProposalStatus.VOIDED
            assert await runtime.ledger.claim(first.id) is None
            await runtime.ledger.save_manual(retry, retry_risk, {}, lease=lease, turn_id=turn_id)
            assert (await store.get_proposal(retry.id)).status is ProposalStatus.PENDING_APPROVAL

    asyncio.run(run())
