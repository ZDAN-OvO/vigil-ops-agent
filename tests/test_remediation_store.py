"""在独立 PostgreSQL schema 中验证处置授权、锁组与接管。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from aiops.actions.remediation_alerts import (
    claim_alert,
    clearance_state,
    complete_alert,
    link_firing,
    record_firing,
    record_resolved,
)
from aiops.actions.remediation_runtime import RemediationRuntime
from aiops.actions.remediation_store import RemediationStore, resource_keys
from aiops.actions.rollout_undo import ActionError
from aiops.actions.safety import ActionSafety
from aiops.contracts import (
    ActionAttempt,
    ActionKind,
    ActionOrigin,
    ActionProposal,
    ActionTarget,
    ApprovalDecision,
    AttemptOutcome,
    AttemptStatus,
    FollowupStatus,
    ProposalStatus,
    RiskDecision,
    RiskDisposition,
)
from aiops.db import Store
from aiops.sessions.models import LeaseLost

BEFORE = {"value": "normal"}


@asynccontextmanager
async def _ledgers(dsn):
    async with Store.open(dsn) as store, Store.open(dsn) as other:
        await store.migrate()
        yield (
            RemediationStore(store, ActionSafety(store)),
            RemediationStore(other, ActionSafety(other)),
        )


async def _proposal(ledger, *, workload="checkout", config_map="checkout-config", manual=False):
    session = await ledger.sessions.create_session(title="核对结算服务")
    await ledger.sessions.enqueue_input(session.id, "核对结算服务", display_text="核对结算服务")
    lease = await ledger.sessions.acquire_main_lease(session.id, "test")
    turn = await ledger.sessions.start_main_turn(lease)
    await ledger.sessions.release_lease(lease)
    identifier = f"act-{uuid4().hex}"
    return ActionProposal(
        id=identifier,
        kind=ActionKind.UPDATE_CONFIG if config_map else ActionKind.SCALE_REPLICAS,
        target=ActionTarget(
            cluster="lab",
            namespace="payments",
            kind="Deployment",
            name=workload,
            uid=f"uid-{workload}",
            resource_version="10",
        ),
        parameters=(
            {"source": "config_map", "config_map": config_map, "key": "MODE", "value": "limited"}
            if config_map
            else {"replicas": 3}
        ),
        reason="按处置规则调整结算服务",
        created_at=datetime.now(UTC),
        idempotency_key=identifier,
        session_id=session.id,
        session_turn_id=turn.id,
        origin=ActionOrigin.OPERATOR if manual else ActionOrigin.RULES,
        evidence_ids=[] if manual else ["obs-rule"],
        status=ProposalStatus.PENDING_APPROVAL if manual else ProposalStatus.APPROVED,
    )


def _risk(proposal):
    return RiskDecision(
        proposal_id=proposal.id,
        disposition=(
            RiskDisposition.APPROVAL
            if proposal.origin is ActionOrigin.OPERATOR
            else RiskDisposition.AUTO
        ),
        policy_version="test-policy",
        reasons=["已核对处置证据"],
        requires_compensation=True,
    )


async def _fault(ledger, target):
    fault_id = uuid4().hex
    async with ledger.store.transaction() as conn:
        await conn.execute(
            """
            INSERT INTO remediation_faults
                (id, cluster, namespace, workload_kind, workload_name, workload_uid)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (fault_id, target.cluster, target.namespace, target.kind, target.name, target.uid),
        )
    return fault_id


async def _create(ledger, proposal, fault_id=None):
    async with ledger.store.transaction() as conn:
        if fault_id is not None:
            await conn.execute(
                """
                INSERT INTO remediation_alerts
                    (cluster, group_key, fingerprint, starts_at, labels,
                     session_id, fault_id, evaluated_at)
                VALUES (%s, %s, %s, clock_timestamp(), '{}', %s, %s, clock_timestamp())
                """,
                (proposal.target.cluster, proposal.id, uuid4().hex, proposal.session_id, fault_id),
            )
        await ledger.create(conn, proposal, _risk(proposal), {"before": BEFORE}, fault_id=fault_id)
        await ledger.publish_rule(conn, proposal)


def _intent(proposal):
    return ActionAttempt(
        id=f"att-{uuid4().hex}",
        proposal_id=proposal.id,
        status=AttemptStatus.INTENT,
        idempotency_key=proposal.idempotency_key,
        snapshot={"before": BEFORE},
        request=proposal.parameters,
        started_at=datetime.now(UTC),
    )


async def _grant(ledger, claim):
    return await ledger.grant_send(claim, _intent(claim.proposal), authorize=Mock())


async def _rows(ledger, query, params=()):
    async with ledger.store.transaction() as conn:
        return await (await conn.execute(query, params)).fetchall()


async def _job(ledger, proposal):
    [job] = await _rows(
        ledger, "SELECT * FROM remediation_jobs WHERE proposal_id = %s", (proposal.id,)
    )
    return job


async def _locks(ledger):
    return await _rows(
        ledger,
        "SELECT target_key, proposal_id, owner, generation, expires_at "
        "FROM action_locks ORDER BY target_key",
    )


def _assert_lock_group(locks, proposal, *, owner, generation, expires_at):
    assert [lock["target_key"] for lock in locks] == resource_keys(proposal)
    assert {
        (row["proposal_id"], row["owner"], row["generation"], row["expires_at"]) for row in locks
    } == {(proposal.id, owner, generation, expires_at)}


async def _expire(ledger, proposal):
    async with ledger.store.transaction() as conn:
        for table in ("remediation_jobs", "action_locks"):
            await conn.execute(
                f"UPDATE {table} SET expires_at = clock_timestamp() - interval '1 second' "
                "WHERE proposal_id = %s AND owner IS NOT NULL",
                (proposal.id,),
            )


async def _finish(ledger, claim, *, compensation=None):
    if compensation is None and claim.job["effective_at"] is not None:
        async with ledger.store.transaction() as conn:
            await conn.execute(
                """
                UPDATE remediation_alerts SET resolved_at = clock_timestamp()
                WHERE fault_id = %s AND starts_at < %s
                """,
                (claim.job["fault_id"], claim.job["effective_at"]),
            )
    await ledger.finish(
        claim,
        outcome=AttemptOutcome.WORSENED if compensation else AttemptOutcome.IMPROVED,
        reason="核对业务探针结果",
        result={"written": True},
        verification={"slots_collected": 10},
        compensation=compensation,
    )


def _compensation(proposal):
    identifier = f"act-{uuid4().hex}"
    child = proposal.model_copy(
        update={
            "id": identifier,
            "idempotency_key": identifier,
            "parameters": {**proposal.parameters, **BEFORE},
            "reason": "恢复处置前的配置",
        }
    )
    return child, _risk(child), {"before": proposal.parameters}


async def _prepare_compensation(ledger):
    parent = await _proposal(ledger)
    await _create(ledger, parent, await _fault(ledger, parent.target))
    claim = await ledger.claim(parent.id)
    await _grant(ledger, claim)
    await ledger.applied(claim, {"receipt": {"resource_version": "11"}})
    observing = await ledger.claim(parent.id)
    return observing, _compensation(parent)


def test_conflict_between_lock_group_read_and_insert_rolls_back_both_keys(
    isolated_serve_dsn, monkeypatch
):
    async def run():
        async with _ledgers(isolated_serve_dsn) as (left, right):
            config = await _proposal(left)
            scale = await _proposal(right, config_map=None)
            fault = await _fault(left, config.target)
            await _create(left, config, fault)
            await _create(right, scale, fault)
            config_key, workload_key = resource_keys(config)
            assert resource_keys(scale) == [workload_key]

            records_read, winner_committed = asyncio.Event(), asyncio.Event()
            read_records = left._records

            async def pause_after_read(*args):
                records = await read_records(*args)
                assert records[-1] == []
                records_read.set()
                await asyncio.wait_for(winner_committed.wait(), timeout=5)
                return records

            async def claim_workload():
                await asyncio.wait_for(records_read.wait(), timeout=5)
                try:
                    claim = await right.claim(scale.id)
                    assert claim is not None
                    return claim
                finally:
                    winner_committed.set()

            monkeypatch.setattr(left, "_records", pause_after_read)
            losing, winner = await asyncio.gather(
                left.claim(config.id), claim_workload(), return_exceptions=True
            )
            assert isinstance(losing, ActionError) and losing.code == "target_busy"
            assert not isinstance(winner, BaseException)
            [held] = await _locks(right)
            assert (held["target_key"], held["proposal_id"]) == (workload_key, scale.id)
            assert (await _job(left, config))["owner"] is None

            another = await _proposal(right, workload="billing")
            await _create(right, another, await _fault(right, another.target))
            assert await right.claim(another.id) is not None
            assert any(
                row["target_key"] == config_key and row["proposal_id"] == another.id
                for row in await _locks(right)
            )

    asyncio.run(run())


def test_expired_send_grant_becomes_unknown_and_retains_both_locks(isolated_serve_dsn):
    async def run():
        async with _ledgers(isolated_serve_dsn) as (sender, restarted):
            proposal = await _proposal(sender)
            fault = await _fault(sender, proposal.target)
            await _create(sender, proposal, fault)
            claim = await sender.claim(proposal.id)
            granted = await _grant(sender, claim)
            assert granted.send_granted_at is not None
            assert await restarted.claim(proposal.id) is None
            assert (
                await restarted.store.get_attempt(proposal.id)
            ).status is AttemptStatus.EXECUTING

            await _expire(sender, proposal)
            held = await _locks(sender)
            assert [row["target_key"] for row in held] == resource_keys(proposal)
            assert await asyncio.gather(
                sender.claim(proposal.id), restarted.claim(proposal.id)
            ) == [None, None]
            assert (await _job(restarted, proposal))["state"] == "finished"
            assert (
                await restarted.store.get_proposal(proposal.id)
            ).status is ProposalStatus.UNKNOWN
            recovered = await restarted.store.get_attempt(proposal.id)
            assert recovered.status is AttemptStatus.UNKNOWN
            assert (recovered.id, recovered.snapshot, recovered.send_granted_at) == (
                granted.id,
                granted.snapshot,
                granted.send_granted_at,
            )
            [followup] = await restarted.store.list_followups(proposal_id=proposal.id)
            assert (followup.attempt_id, followup.outcome, followup.status) == (
                granted.id,
                AttemptOutcome.UNKNOWN,
                FollowupStatus.OPEN,
            )
            assert await _locks(restarted) == held
            with pytest.raises(LeaseLost):
                await sender.applied(claim, {"receipt": "late"})
            contender = await _proposal(restarted)
            await _create(restarted, contender, fault)
            assert await restarted.claim(contender.id) is None
            assert await _locks(restarted) == held

    asyncio.run(run())


def test_confirmed_write_restarts_in_observation_and_fences_old_owner(
    isolated_serve_dsn, monkeypatch
):
    async def run():
        async with _ledgers(isolated_serve_dsn) as (sender, restarted):
            proposal = await _proposal(sender)
            await _create(sender, proposal, await _fault(sender, proposal.target))
            first = await sender.claim(proposal.id)
            granted = await _grant(sender, first)
            write_receipt = {"receipt": {"resource_version": "11"}}
            await sender.applied(first, write_receipt)
            applied_at = (await _job(restarted, proposal))["applied_at"]
            assert applied_at is not None
            assert await restarted.due() == [proposal.id]

            runtime = RemediationRuntime(None, restarted, None, None, None)
            send, observe = AsyncMock(), AsyncMock()
            monkeypatch.setattr(runtime, "_send", send)
            monkeypatch.setattr(runtime, "_observe", observe)
            await runtime.drive(proposal.id)
            send.assert_not_awaited()
            observe.assert_awaited_once()
            (second,) = observe.await_args.args
            assert second.job["state"] == "applying"
            assert second.job["applied_at"] == applied_at
            assert second.job["generation"] > first.job["generation"]
            assert second.job["owner"] != first.job["owner"]
            assert second.attempt.id == granted.id
            assert second.attempt.result == {"stage": "applied", "written": True, **write_receipt}
            with pytest.raises(LeaseLost):
                await sender.renew(first)
            with pytest.raises(ActionError) as refused:
                await _grant(restarted, second)
            assert refused.value.code == "not_approved"

            sample = {
                "slots_collected": 3,
                "failed_slots": 0,
                "business_recovered_at": (applied_at + timedelta(seconds=102)).isoformat(),
                "fault_pods": {
                    "new-pod": {"container_id": "containerd://container", "restarts": 0}
                },
            }
            await restarted.reschedule(second, delay=0, effective_at=applied_at, observation=sample)
            observer = await sender.claim(proposal.id)
            assert observer.job["state"] == "observing"
            assert observer.job["effective_at"] == applied_at
            assert observer.job["observation"] == sample
            assert observer.attempt.id == granted.id
            await _finish(sender, observer)
            assert (await restarted.store.get_attempt(proposal.id)).status is AttemptStatus.VERIFIED
            assert await _locks(restarted) == []

    asyncio.run(run())


@pytest.mark.parametrize("scenario", ["fast_resolved", "late_active", "previous_fault"])
def test_cross_group_alerts_settle_with_the_correct_fault(isolated_serve_dsn, scenario):
    async def run():
        async with _ledgers(isolated_serve_dsn) as (ledger, observer):
            proposal = await _proposal(ledger)
            fault_id = await _fault(ledger, proposal.target)
            await _create(ledger, proposal, fault_id)
            first = await ledger.claim(proposal.id)
            await _grant(ledger, first)
            await ledger.applied(first, {"receipt": {"resource_version": "11"}})
            applying = await observer.claim(proposal.id)
            effective_at = applying.job["applied_at"]
            await observer.reschedule(
                applying,
                delay=0,
                effective_at=effective_at,
                observation={
                    "business_recovered_at": (effective_at + timedelta(seconds=102)).isoformat(),
                },
            )
            previous_clearance = effective_at - timedelta(minutes=4)
            late_session = await ledger.sessions.create_session(title="跨组告警")
            async with ledger.store.transaction() as conn:
                await conn.execute(
                    "UPDATE remediation_alerts SET starts_at = %s, resolved_at = clock_timestamp() "
                    "WHERE fault_id = %s",
                    (effective_at - timedelta(minutes=3), fault_id),
                )
                if scenario == "previous_fault":
                    target = proposal.target
                    await conn.execute(
                        """
                        INSERT INTO remediation_faults
                            (id, cluster, namespace, workload_kind, workload_name, workload_uid,
                             opened_at, cleared_at)
                        VALUES ('previous-fault', %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            target.cluster,
                            target.namespace,
                            target.kind,
                            target.name,
                            target.uid,
                            effective_at - timedelta(minutes=8),
                            previous_clearance,
                        ),
                    )
                event = {
                    "cluster": proposal.target.cluster,
                    "group_key": "another-notification-group",
                    "fingerprint": "0000000000000002",
                    "starts_at": {
                        "fast_resolved": effective_at + timedelta(seconds=1),
                        "late_active": effective_at - timedelta(minutes=2),
                        "previous_fault": previous_clearance - timedelta(seconds=1),
                    }[scenario],
                    "labels": {
                        "namespace": proposal.target.namespace,
                        "target": proposal.target.name,
                    },
                }
                assert await record_firing(conn, **event)
                await link_firing(
                    conn,
                    **{key: value for key, value in event.items() if key != "labels"},
                    session_id=late_session.id,
                    evaluate=True,
                )
                if scenario == "fast_resolved":
                    await record_resolved(conn, **event)

            observing = await observer.claim(proposal.id)
            outcome = await observer.finish(
                observing,
                outcome=AttemptOutcome.IMPROVED,
                reason="探针通过",
                result={"written": True},
                verification={"slots_collected": 10},
            )
            old_firing = scenario == "previous_fault"
            assert outcome is (
                AttemptOutcome.IMPROVED if old_firing else AttemptOutcome.NO_IMPROVEMENT
            )
            saved = await ledger.store.get_proposal(proposal.id)
            assert saved.status is (
                ProposalStatus.SUCCEEDED if old_firing else ProposalStatus.FAILED
            )
            [fault] = await _rows(
                ledger,
                "SELECT cleared_at FROM remediation_faults WHERE id = %s",
                (fault_id,),
            )
            assert (fault["cleared_at"] is not None) == old_firing

    asyncio.run(run())


def test_compensation_handoff_is_atomic(isolated_serve_dsn, monkeypatch):
    async def run():
        async with _ledgers(isolated_serve_dsn) as (first, restarted):
            observing, compensation = await _prepare_compensation(first)
            parent = observing.proposal
            old_locks = await _locks(restarted)
            child, _, _ = compensation

            pending_commit, allow_commit = asyncio.Event(), asyncio.Event()
            publish = first.publish_rule

            async def pause_before_commit(conn, proposal):
                await publish(conn, proposal)
                pending_commit.set()
                await asyncio.wait_for(allow_commit.wait(), timeout=5)

            async def read_before_commit():
                await asyncio.wait_for(pending_commit.wait(), timeout=5)
                try:
                    assert await _locks(restarted) == old_locks
                    assert await restarted.store.get_proposal(child.id) is None
                    assert (await _job(restarted, parent))["state"] == "applying"
                finally:
                    allow_commit.set()

            monkeypatch.setattr(first, "publish_rule", pause_before_commit)
            await asyncio.gather(
                _finish(first, observing, compensation=compensation), read_before_commit()
            )
            transferred = await _locks(restarted)
            assert len(transferred) == 2
            custody = transferred[0]
            assert custody["owner"] != observing.job["owner"]
            assert custody["generation"] > observing.job["generation"]
            _assert_lock_group(
                transferred,
                child,
                owner=custody["owner"],
                generation=custody["generation"],
                expires_at=custody["expires_at"],
            )
            assert (await _job(restarted, child))["parent_proposal_id"] == parent.id
            assert (await _job(restarted, parent))["state"] == "finished"

    asyncio.run(run())


def test_expired_compensation_is_taken_over_once_and_keeps_locks_on_unknown(isolated_serve_dsn):
    async def run():
        async with _ledgers(isolated_serve_dsn) as (first, restarted):
            observing, compensation = await _prepare_compensation(first)
            child, _, _ = compensation
            await _finish(first, observing, compensation=compensation)

            former = await first.claim(child.id)
            assert former is not None
            await _expire(first, child)
            with pytest.raises(LeaseLost):
                await first.renew(former)
            current = await restarted.claim(child.id)
            assert current.job["generation"] > former.job["generation"]
            with pytest.raises(LeaseLost):
                await _grant(first, former)
            await restarted.renew(current)
            renewed = await _job(restarted, child)
            _assert_lock_group(
                await _locks(first),
                child,
                owner=current.job["owner"],
                generation=current.job["generation"],
                expires_at=renewed["expires_at"],
            )
            granted = await _grant(restarted, current)
            assert granted.send_granted_at is not None
            await _expire(restarted, child)
            assert await first.claim(child.id) is None
            recovered = await first.store.get_attempt(child.id)
            assert (recovered.id, recovered.status) == (granted.id, AttemptStatus.UNKNOWN)
            locks = await _locks(first)
            assert [row["target_key"] for row in locks] == resource_keys(child)
            assert {row["proposal_id"] for row in locks} == {child.id}

    asyncio.run(run())


def test_fault_allows_one_automatic_send_and_one_compensation_send(isolated_serve_dsn):
    async def run():
        async with _ledgers(isolated_serve_dsn) as (first, other):
            parent = await _proposal(first)
            fault = await _fault(first, parent.target)
            await _create(first, parent, fault)
            initial = await first.claim(parent.id)
            await _grant(first, initial)
            await first.applied(initial, {"receipt": {"resource_version": "11"}})
            compensation = _compensation(parent)
            await _finish(first, await first.claim(parent.id), compensation=compensation)
            child, _, _ = compensation
            child_claim = await other.claim(child.id)
            await _grant(other, child_claim)
            with pytest.raises(ActionError) as repeated:
                await _grant(other, child_claim)
            assert repeated.value.code == "not_approved"
            await other.applied(child_claim, {"receipt": {"resource_version": "12"}})
            await _finish(other, await other.claim(child.id))

            later = await _proposal(other)
            await _create(other, later, fault)
            later_claim = await other.claim(later.id)
            with pytest.raises(ActionError) as refused:
                await _grant(other, later_claim)
            assert refused.value.code == "fault_quota"
            assert await other.store.get_attempt(later.id) is None
            await other.fail_unsent(later_claim, refused.value.code, "原故障名额已用完")

            # 使用另一父记录，确保拒绝来自故障补偿名额，而非父记录的唯一约束。
            duplicate, risk, evidence = _compensation(later)
            async with first.store.transaction() as conn:
                await first.create(
                    conn, duplicate, risk, evidence, fault_id=fault, parent_id=later.id
                )
                with pytest.raises(UniqueViolation) as rejected:
                    async with conn.transaction():
                        await conn.execute(
                            "UPDATE remediation_jobs SET sent_at = clock_timestamp() "
                            "WHERE proposal_id = %s",
                            (duplicate.id,),
                        )
                assert rejected.value.diag.constraint_name == "remediation_one_automatic_send"
            assert (await _job(first, duplicate))["sent_at"] is None
            assert await _rows(
                first,
                "SELECT proposal_id, parent_proposal_id FROM remediation_jobs "
                "WHERE fault_id = %s AND automated AND sent_at IS NOT NULL ORDER BY sent_at",
                (fault,),
            ) == [
                {"proposal_id": parent.id, "parent_proposal_id": None},
                {"proposal_id": child.id, "parent_proposal_id": parent.id},
            ]

    asyncio.run(run())


@pytest.mark.parametrize("point", ["before_approval", "after_claim"])
def test_card_fingerprint_change_cannot_grant_a_send(isolated_serve_dsn, point):
    async def run():
        async with _ledgers(isolated_serve_dsn) as (performer, editor):
            proposal = await _proposal(performer, manual=True)
            await _create(performer, proposal)
            card = await performer.safety.get_card(proposal.id, session_id=proposal.session_id)
            assert card is not None

            async def approve():
                return await performer.safety.record_card_decision(
                    proposal.id,
                    session_id=proposal.session_id,
                    fingerprint=card["fingerprint"],
                    decision=ApprovalDecision.APPROVED,
                    actor="operator",
                )

            claim = None
            if point == "after_claim":
                approved = await approve()
                assert approved.proposal_hash == card["fingerprint"]
                claim = await performer.claim(proposal.id)
                assert claim is not None
            changed = {**proposal.parameters, "value": "maintenance"}
            await _rows(
                editor,
                "UPDATE action_proposals SET parameters = %s WHERE id = %s RETURNING id",
                (Jsonb(changed), proposal.id),
            )

            authorize = Mock()
            if claim is None:
                with pytest.raises(ActionError) as refused:
                    await approve()
                assert refused.value.code == "hash_mismatch"
                assert await performer.claim(proposal.id) is None
                assert await performer.store.get_approval(proposal.id) is None
                assert await _locks(editor) == []
            else:
                with pytest.raises(ActionError) as refused:
                    await performer.grant_send(claim, _intent(proposal), authorize=authorize)
                assert refused.value.code == "no_approval"
                authorize.assert_not_called()
                await performer.fail_unsent(claim, refused.value.code, "审批摘要已变化")
                assert await _locks(editor) == []
            assert (await _job(editor, proposal))["sent_at"] is None
            assert await editor.store.get_attempt(proposal.id) is None
            assert not await _rows(
                editor,
                "SELECT 1 FROM audit_events "
                "WHERE correlation_id = %s AND event = 'action.send_granted'",
                (proposal.id,),
            )

    asyncio.run(run())


def test_old_resolution_does_not_close_new_alert_and_only_one_worker_claims_it(isolated_serve_dsn):
    async def run():
        async with _ledgers(isolated_serve_dsn) as (first, second):
            session = await first.sessions.create_session(title="结算告警")
            latest = datetime.now(UTC)
            alert = {
                "cluster": "lab",
                "group_key": "checkout",
                "fingerprint": "f0",
                "starts_at": latest,
                "labels": {"alertname": "CheckoutErrors"},
            }
            old = {**alert, "starts_at": latest - timedelta(minutes=10)}
            async with first.store.transaction() as conn:
                assert await record_firing(conn, **alert)
                await link_firing(
                    conn,
                    **{key: value for key, value in alert.items() if key != "labels"},
                    session_id=session.id,
                    evaluate=True,
                )
                await record_resolved(conn, **old)
                assert not await record_firing(conn, **old)

            claims = await asyncio.gather(claim_alert(first.store), claim_alert(second.store))
            [active] = [claim for claim in claims if claim is not None]
            assert active["starts_at"] == latest
            async with second.store.transaction() as conn:
                assert await clearance_state(conn, session.id) == (True, True, True)
                assert await complete_alert(conn, active)
                assert await clearance_state(conn, session.id) == (True, True, False)
                await record_resolved(conn, **alert)
                assert await clearance_state(conn, session.id) == (True, False, False)

    asyncio.run(run())
