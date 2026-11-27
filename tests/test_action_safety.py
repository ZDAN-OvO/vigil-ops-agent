"""动作权限、收尾及切换使用真实 PostgreSQL 与受控集群替身。"""

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from actions_support import (
    DSN,
    FakeAdapter,
    FakePrometheus,
    FakeRestartAdapter,
    build_executor,
    legacy_risk,
    make_proposal,
    pod_plan,
    restart_proposal,
    rollout_plan,
    seed_legacy_approval,
    seed_legacy_attempt,
    seed_legacy_card,
)
from controlled_action_support import (
    expire_job,
    finish_observation,
    intent_for,
    prepare_replica,
    replica_runtime,
)

from aiops.actions.kubernetes_write import WRITE_TIMEOUT_SECONDS
from aiops.actions.policy import target_key
from aiops.actions.reconciliation import ActionReconciler, LegacyActionCutover
from aiops.actions.rollout_undo import ActionError
from aiops.actions.safety import ActionSafety, Reservation
from aiops.contracts import ActionAttempt, ApprovalDecision, AttemptStatus, ProposalStatus
from aiops.db import Store
from aiops.sessions.models import LeaseLost
from aiops.sessions.projection import project_action_result, project_payload, project_template_diff
from aiops.sessions.store import SessionStore

pytestmark = pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL")


async def _legacy_executor(store, tmp_path, cleanup, adapter):
    await store.migrate()
    return await build_executor(
        store, adapter, FakePrometheus([200.0, 200.0]), tmp_path, cleanup=cleanup
    )


async def _new(store, tmp_path, cleanup, **kwargs):
    await store.migrate()
    executor = await build_executor(
        store,
        FakeAdapter(rollout_plan()),
        FakePrometheus([200.0, 200.0]),
        tmp_path,
        cleanup=cleanup,
        **kwargs,
    )
    runtime = replica_runtime(store)
    proposal = await prepare_replica(executor, runtime, reason="凭据 token=synthetic-key")
    cleanup.append(proposal.id)
    return executor, proposal, runtime


async def _approved(store, tmp_path, cleanup, **kwargs):
    executor, proposal, runtime = await _new(store, tmp_path, cleanup, **kwargs)
    event = await executor.publish_test_card(proposal.id)
    approval = await ActionSafety(store).record_card_decision(
        proposal.id,
        session_id=proposal.session_id,
        fingerprint=event.payload["fingerprint"],
        decision=ApprovalDecision.APPROVED,
        actor="tester",
    )
    return executor, proposal, runtime, approval


def _key(proposal):
    target = proposal.target
    return target_key(target.cluster, target.namespace, target.kind, target.name)


async def _expire(store, proposal):
    async with store.transaction() as conn:
        await conn.execute(
            "UPDATE sessions SET lease_expires_at = clock_timestamp() - interval '1 second' "
            "WHERE id = %s",
            (proposal.session_id,),
        )
        await conn.execute(
            "UPDATE action_locks SET expires_at = clock_timestamp() - %s * interval '1 second' "
            "WHERE proposal_id = %s",
            (WRITE_TIMEOUT_SECONDS + 1, proposal.id),
        )


def test_card_is_bound_to_pause_scope_and_saved_fingerprint(tmp_path, cleanup):
    async def run():
        async with Store.open(DSN) as store:
            executor, proposal, runtime = await _new(
                store,
                tmp_path,
                cleanup,
                web_url="http://127.0.0.1:8090",
            )
            safety = ActionSafety(store)
            lease = executor._session_lease
            with pytest.raises(ActionError) as caught:
                await executor.publish_card(
                    proposal.id, lease=lease, turn_id=proposal.session_turn_id
                )
            assert caught.value.code == "checkpoint_missing"
            assert await safety.get_card(proposal.id, session_id=proposal.session_id) is None
            card = await executor.publish_test_card(proposal.id)
            assert card == await executor.publish_card(
                proposal.id,
                lease=lease,
                turn_id=proposal.session_turn_id,
            )
            assert card.payload["url"].endswith(
                f"/sessions/{proposal.session_id}#card-{proposal.id}"
            )
            assert card.payload["action"] == "scale_replicas"
            assert card.payload["parameters"] == {"replicas": 2}
            assert card.payload["before"] == {"replicas": 1}
            assert "synthetic-key" not in json.dumps(card.payload)
            assert await safety.list_runnable_card_sessions() == []
            await SessionStore(store).release_lease(lease)
            assert await safety.list_runnable_card_sessions() == []
            with pytest.raises(ActionError) as caught:
                await safety.record_card_decision(
                    proposal.id,
                    session_id=proposal.session_id,
                    fingerprint="wrong",
                    decision=ApprovalDecision.APPROVED,
                    actor="tester",
                )
            assert caught.value.code == "hash_mismatch"
            rejected = await safety.record_card_decision(
                proposal.id,
                session_id=proposal.session_id,
                fingerprint=card.payload["fingerprint"],
                decision=ApprovalDecision.REJECTED,
                actor="tester",
            )
            assert (
                await executor.get_card_decision(proposal.id, session_id=proposal.session_id)
                == rejected
            )
            assert proposal.session_id in await safety.list_runnable_card_sessions()
            assert (await safety.get_card(proposal.id, session_id=proposal.session_id))[
                "status"
            ] == "rejected"
            assert await store.get_attempt(proposal.id) is None
            events = await SessionStore(store).list_events(proposal.session_id, after_seq=card.seq)
            (decision,) = [event for event in events if event.type == "card_decision"]
            assert decision.payload["expires_at"] is None
            assert runtime.kube.dry_runs == runtime.kube.writes == []

    asyncio.run(run())


def test_close_revokes_approval_after_job_claim(tmp_path, cleanup):
    async def run():
        async with Store.open(DSN) as driver, Store.open(DSN) as service:
            _, proposal, runtime, _ = await _approved(
                driver, tmp_path, cleanup, group_key=f"alert-{uuid4().hex}"
            )
            claim = await runtime.ledger.claim(proposal.id)
            assert claim is not None
            sessions = SessionStore(service)
            session = await sessions.get_session(proposal.session_id)
            async with sessions.alert_transaction(session.group_key) as conn:
                event = await ActionSafety(service).close_session(conn, session.id, reason="已恢复")
                assert event.type == "session_closed"
            with pytest.raises((ActionError, LeaseLost)):
                await runtime.ledger.grant_send(
                    claim, intent_for(proposal), authorize=lambda *_: pytest.fail("会话已关闭")
                )
            assert not await service.target_is_locked(_key(proposal))
            assert (await service.get_proposal(proposal.id)).status == ProposalStatus.VOIDED
            assert (await service.get_approval(proposal.id)).revoked_at is not None
            assert await service.get_attempt(proposal.id) is None
            assert runtime.kube.writes == []
            events = await sessions.list_events(session.id)
            (receipt,) = [event for event in events if event.type == "action_result"]
            assert receipt.payload["written"] is False

    asyncio.run(run())


@pytest.mark.parametrize("close", [False, True])
def test_observation_is_taken_over_after_session_lease_ends(tmp_path, cleanup, close):
    async def run():
        async with Store.open(DSN) as driver, Store.open(DSN) as service:
            executor, proposal, original, _ = await _approved(driver, tmp_path, cleanup)
            await original.drive(proposal.id)
            await original.drive(proposal.id)
            first = await service.get_attempt(proposal.id)
            assert first.status is AttemptStatus.EXECUTING
            assert original.kube.writes == [{"replicas": 2}]
            assert await service.target_is_locked(_key(proposal))
            sessions = SessionStore(service)
            if close:
                async with service.transaction() as conn:
                    await ActionSafety(service).close_session(
                        conn, proposal.session_id, reason="关闭"
                    )
            else:
                await sessions.release_lease(executor._session_lease)
                assert await sessions.acquire_main_lease(proposal.session_id, "successor")
            successor = replica_runtime(service, kube=original.kube)
            await finish_observation(successor, proposal.id)
            result = await service.get_attempt(proposal.id)
            assert result.id == first.id
            assert result.status is AttemptStatus.VERIFIED
            assert original.kube.writes == [{"replicas": 2}]
            assert not await service.target_is_locked(_key(proposal))

    asyncio.run(run())


@pytest.mark.parametrize("checkpoint", ["claim", "send_grant"])
def test_new_job_recovery_distinguishes_claim_and_send_grant(tmp_path, cleanup, checkpoint):
    async def run():
        async with Store.open(DSN) as driver, Store.open(DSN) as recovery:
            _, proposal, original, _ = await _approved(driver, tmp_path, cleanup)
            claim = await original.ledger.claim(proposal.id)
            assert claim is not None
            if checkpoint == "send_grant":
                await original.ledger.grant_send(
                    claim, intent_for(proposal), authorize=lambda *_: None
                )
            await expire_job(recovery, proposal.id)
            successor = replica_runtime(recovery)
            next_claim = await successor.ledger.claim(proposal.id)
            if checkpoint == "claim":
                assert next_claim is not None
                assert next_claim.job["generation"] > claim.job["generation"]
                with pytest.raises(LeaseLost):
                    await original.ledger.renew(claim)
                assert await recovery.get_attempt(proposal.id) is None
            else:
                assert next_claim is None
                receipt = await recovery.get_attempt(proposal.id)
                assert receipt.status is AttemptStatus.UNKNOWN
                assert project_action_result(receipt)["written"] is None
                assert await recovery.target_is_locked(_key(proposal))
                assert await successor.ledger.claim(proposal.id) is None
            assert original.kube.writes == successor.kube.writes == []

    asyncio.run(run())


@pytest.mark.parametrize("checkpoint", ["intent", "send_grant"])
@pytest.mark.parametrize("proposal_factory", [make_proposal, restart_proposal])
def test_legacy_send_is_disabled_and_recovery_prunes_only_unsent(
    tmp_path, cleanup, proposal_factory, checkpoint
):
    async def run():
        adapter = FakeAdapter(rollout_plan())
        async with Store.open(DSN) as store:
            executor = await _legacy_executor(store, tmp_path, cleanup, adapter)
            proposal, card = await seed_legacy_card(
                store, executor, cleanup, proposal_factory=proposal_factory
            )
            safety = ActionSafety(store)
            displayed = await safety.get_card(proposal.id, session_id=proposal.session_id)
            assert displayed["action"] == proposal.kind
            assert displayed["status"] == "voided"
            assert displayed["fingerprint"] == card.payload["fingerprint"]
            with pytest.raises(ActionError) as caught:
                await safety.record_card_decision(
                    proposal.id,
                    session_id=proposal.session_id,
                    fingerprint=card.payload["fingerprint"],
                    decision=ApprovalDecision.APPROVED,
                    actor="tester",
                )
            assert caught.value.code == "policy_denied"
            assert await store.get_approval(proposal.id) is None
            approval = await seed_legacy_approval(store, proposal)
            sent = checkpoint == "send_grant"
            attempt = await seed_legacy_attempt(
                store,
                proposal,
                status=AttemptStatus.EXECUTING if sent else AttemptStatus.INTENT,
                lock=True,
            )
            if sent:
                await store.set_proposal_status(proposal.id, ProposalStatus.EXECUTING)
            else:
                lease, turn_id = executor._scope()
                async with store.transaction() as conn:
                    lock = await safety._lock(conn, _key(proposal))
                reservation = Reservation(
                    proposal.id, _key(proposal), lock["owner"], lock["generation"], lease, turn_id
                )
                with pytest.raises(ActionError) as caught:
                    await safety.grant_send(reservation, attempt, approval)
                assert caught.value.code == "policy_denied"
                assert (await store.get_attempt(proposal.id)).send_granted_at is None
            assert (
                await safety.get_card_decision(proposal.id, session_id=proposal.session_id)
                == approval
            )
            await _expire(store, proposal)
            await safety.recover_target(_key(proposal))
            saved = await store.get_proposal(proposal.id)
            receipt = await store.get_attempt(proposal.id)
            if sent:
                assert saved.status is ProposalStatus.UNKNOWN
                assert receipt.status is AttemptStatus.UNKNOWN
                assert project_action_result(receipt)["written"] is None
                assert await store.target_is_locked(_key(proposal))
            else:
                assert saved.status is ProposalStatus.VOIDED
                assert receipt.status is AttemptStatus.VOIDED
                assert project_action_result(receipt)["written"] is False
                assert (await store.get_approval(proposal.id)).revoked_at is not None
                assert not await store.target_is_locked(_key(proposal))
            assert adapter.calls == []

    asyncio.run(run())


def test_human_verification_fences_late_rollback_and_keeps_lock_on_failure(tmp_path, cleanup):
    class LostResponse(FakeAdapter):
        version = "10"
        fence = None
        fail_isolation = True
        sends = 0
        observing = False

        def apply(self, plan, *, dry_run, proposal_id):
            if dry_run:
                return {}
            self.sends += 1
            raise TimeoutError("cookie=definitely-private")

        def late_write(self):
            if self.version != self._plan.resource_version:
                raise ActionError("conflict", "原 resourceVersion 已失效")
            self.version = "11"

        def isolate(self, namespace, name, uid, version, token):
            if self.fail_isolation:
                raise ActionError("isolation_failed", "模拟隔离失败")
            self.version = "12"
            self.fence = token
            self.observing = True
            return self.version

        def state(self, namespace, name):
            if self.observing:
                self.observing = False
                return {
                    "uid": self._plan.deployment_uid,
                    "resource_version": self.version,
                    "reconciliation_marker": self.fence,
                    "generation": 2,
                    "observed_generation": 1,
                }
            if self.fence is not None:
                self.version = "13"
            return {
                "uid": self._plan.deployment_uid,
                "resource_version": self.version,
                "reconciliation_marker": self.fence,
                "generation": 2,
                "observed_generation": 2,
                "token": "fake-token",
            }

    async def run():
        adapter = LostResponse(rollout_plan())
        async with Store.open(DSN) as store:
            executor = await _legacy_executor(store, tmp_path, cleanup, adapter)
            proposal, _ = await seed_legacy_card(
                store, executor, cleanup, status=ProposalStatus.UNKNOWN
            )
            receipt = await seed_legacy_attempt(
                store,
                proposal,
                status=AttemptStatus.UNKNOWN,
                lock=True,
                result={"stage": "apply", "error": "TimeoutError"},
            )
            assert adapter.sends == 0 and receipt.status == AttemptStatus.UNKNOWN
            reconciler = ActionReconciler(store, adapter=adapter)
            assert (await reconciler.get_reconciliation(proposal.id))["attempt_id"] == receipt.id
            with pytest.raises(ActionError, match="原发送者"):
                await reconciler.prepare_reconciliation(proposal.id, actor="tester")
            await _expire(store, proposal)
            with pytest.raises(ActionError, match="模拟隔离失败"):
                await reconciler.prepare_reconciliation(proposal.id, actor="tester")
            assert await store.target_is_locked(_key(proposal))
            adapter.fail_isolation = False
            prepared = await reconciler.prepare_reconciliation(proposal.id, actor="tester")
            assert "fake-token" not in json.dumps(prepared)
            assert prepared["evidence"]["state"]["resource_version"] == "13"
            with pytest.raises(ActionError, match="原 resourceVersion"):
                adapter.late_write()
            with pytest.raises(ActionError, match="结果不明"):
                await reconciler.reconcile_action(
                    proposal.id,
                    preparation_id=prepared["preparation_id"],
                    actor="tester",
                    written=None,
                    verified=False,
                    note="读取结果不明",
                )
            assert await store.target_is_locked(_key(proposal))
            reconciled = await reconciler.reconcile_action(
                proposal.id,
                preparation_id=prepared["preparation_id"],
                actor="tester",
                written=False,
                verified=False,
                note="读取到原模板",
            )
            assert reconciled.status == AttemptStatus.FAILED
            assert project_action_result(reconciled)["written"] is False
            assert not await store.target_is_locked(_key(proposal))
            with pytest.raises(ActionError, match="原 resourceVersion"):
                adapter.late_write()

    asyncio.run(run())


def test_legacy_cutover_is_explicit_idempotent_and_restores_unknown_lock(tmp_path, cleanup):
    async def run():
        async with Store.open(DSN) as store:
            await store.migrate()
            pending = make_proposal(id=f"act-{uuid4().hex}", idempotency_key=uuid4().hex)
            unknown = make_proposal(
                id=f"act-{uuid4().hex}",
                idempotency_key=uuid4().hex,
                status=ProposalStatus.UNKNOWN,
            )
            cleanup.extend([pending.id, unknown.id])
            await store.save_proposal(pending, legacy_risk(pending))
            await store.save_proposal(unknown, legacy_risk(unknown))
            await store.save_attempt(
                ActionAttempt(
                    id=f"att-{uuid4().hex}",
                    proposal_id=unknown.id,
                    status=AttemptStatus.UNKNOWN,
                    idempotency_key=unknown.idempotency_key,
                    snapshot={"uid": unknown.target.uid},
                    request={},
                    started_at=datetime.now(UTC),
                )
            )
            cutover = LegacyActionCutover(store)
            plan = await cutover.preview()
            assert [
                (item["card_id"], item["classification"])
                for item in plan["actions"]
                if item["card_id"] in cleanup
            ] == sorted([(pending.id, "void"), (unknown.id, "retain")])
            with pytest.raises(ActionError, match="须确认"):
                await cutover.prepare(
                    actor="tester",
                    confirmed=False,
                    old_processes_stopped=True,
                    restart_disabled=True,
                    backup_complete=True,
                )
            assert (await store.get_proposal(pending.id)).status == ProposalStatus.PENDING_APPROVAL
            assert await ActionSafety(store).target_is_locked(_key(unknown))
            assert not await store.target_is_locked(_key(unknown))
            for _ in range(2):
                await cutover.prepare(
                    actor="tester",
                    confirmed=True,
                    old_processes_stopped=True,
                    restart_disabled=True,
                    backup_complete=True,
                )
            assert (await store.get_proposal(pending.id)).status == ProposalStatus.VOIDED
            assert await store.target_is_locked(_key(unknown))
            assert (await store.get_attempt(unknown.id)).result["stage"] == "legacy_cutover"
            assert len(await store.list_followups(proposal_id=unknown.id)) == 1
            executor = await build_executor(
                store,
                FakeAdapter(rollout_plan()),
                FakePrometheus([200.0]),
                tmp_path,
                cleanup=cleanup,
            )
            with pytest.raises(ActionError, match="不属于当前主线"):
                await executor.execute_authorized(unknown.id)

    asyncio.run(run())


def test_shared_projection_redacts_nested_text_urls_and_templates():
    canary = uuid4().hex
    personal_access_token = "github_pat_" + "A" * 82
    payload = {
        "error": (
            "异常: details: token=opaque-value 读取 /home/test/private.txt 失败，"
            "也检查 ./hidden/config"
        ),
        "trace": {"observation": f"dsn=postgres://user:{canary}@example.test/db"},
        "template": {"spec": {"containers": [{"env": [{"value": "opaque-template"}]}]}},
        "url": f"https://fake-user:{canary}@example.test/%2Ftmp%2Fprivate?ticket=opaque-ticket",
        "card": "http://[::1]:8090/sessions/id#card-act-id",
        "login": "http://127.0.0.1:8090/%6cogin/opaque-login",
        "log": f"上游响应包含 {personal_access_token}",
        "detail": {"error": f"请求 {personal_access_token} 失败"},
        "query": f"https://example.test/probe?trace={personal_access_token}",
    }
    projected = project_payload(payload)
    serialized = json.dumps(projected, ensure_ascii=False)
    for secret in (
        "opaque-value",
        "private.txt",
        "./hidden",
        "opaque-template",
        canary,
        "fake-user",
        "opaque-ticket",
        "opaque-login",
        "tmp%2Fprivate",
        personal_access_token,
    ):
        assert secret not in serialized
    assert projected["card"] == payload["card"]
    assert project_payload(projected) == projected


def test_probe_header_values_are_hidden_in_leaf_and_added_subtree():
    current = {"name": "X-API-Key", "value": "fixture-old-probe-key"}
    target = {"name": "X-API-Key", "value": "fixture-new-probe-key"}
    changes = [
        {
            "op": "replace",
            "path": "/spec/containers/0/readinessProbe/httpGet/httpHeaders/0/value",
            "current": current["value"],
            "value": target["value"],
        },
        {
            "op": "add",
            "path": "/spec/containers/0/livenessProbe",
            "value": {"httpGet": {"path": "/ready", "httpHeaders": [target]}},
        },
    ]
    projected = project_template_diff(changes)
    assert [(item["op"], item["path"]) for item in projected] == [
        (item["op"], item["path"]) for item in changes
    ]
    assert projected[1]["value"]["httpGet"]["path"] == "/ready"
    assert "fixture-old-probe-key" not in str(projected)
    assert "fixture-new-probe-key" not in str(projected)


def test_pending_legacy_lease_blocks_cutover_then_voids(cleanup, ids):
    async def run():
        async with Store.open(DSN) as store:
            await store.migrate()
            async with store.transaction() as conn:
                await conn.execute(
                    """
                    INSERT INTO incidents (
                        id, environment, cluster, namespace, service, trigger, symptoms,
                        time_start, time_end
                    ) VALUES (%s, 'test', 'test', 'test', 'test', 'manual', '测试',
                              clock_timestamp() - interval '1 minute', clock_timestamp())
                    """,
                    (ids["incident"],),
                )
                await conn.execute(
                    """
                    INSERT INTO investigations (
                        id, incident_id, status, model, policy_version, budget_tool_calls,
                        budget_model_calls, budget_wall_clock_seconds, started_at
                    ) VALUES (%s, %s, 'running', 'test', 'test', 5, 5, 60, clock_timestamp())
                    """,
                    (ids["investigation"], ids["incident"]),
                )
                await conn.execute(
                    """
                    INSERT INTO investigation_tasks (
                        investigation_id, phase, runtime_version, calendar_started_at,
                        lease_owner, lease_expires_at
                    ) VALUES (%s, 'running', 'legacy', clock_timestamp(),
                              'legacy-owner', clock_timestamp() + interval '60 seconds')
                    """,
                    (ids["investigation"],),
                )
            proposal = make_proposal(id=f"act-{uuid4().hex}", idempotency_key=uuid4().hex)
            cleanup.append(proposal.id)
            await store.save_proposal(proposal, legacy_risk(proposal))
            cutover = LegacyActionCutover(store)
            assert (await cutover.preview())["blocked"]
            flags = dict(
                actor="tester",
                confirmed=True,
                old_processes_stopped=True,
                restart_disabled=True,
                backup_complete=True,
            )
            with pytest.raises(ActionError) as caught:
                await cutover.prepare(**flags)
            assert caught.value.code == "legacy_cutover_blocked"
            assert (await store.get_proposal(proposal.id)).status == ProposalStatus.PENDING_APPROVAL
            async with store.transaction() as conn:
                await conn.execute(
                    "UPDATE investigation_tasks SET lease_expires_at = clock_timestamp() "
                    "WHERE investigation_id = %s",
                    (ids["investigation"],),
                )
            assert (await cutover.preview())["blocked"] is False
            await cutover.prepare(**flags)
            assert (await store.get_proposal(proposal.id)).status == ProposalStatus.VOIDED

    asyncio.run(run())


def test_reconciliation_records_residual_pod_uid_risk(tmp_path, cleanup):
    class UncertainRestart(FakeRestartAdapter):
        def reconciliation_state(self, namespace, name, uid):
            assert uid == self._plan.uid
            return {"original_present": True, "current_uid": uid, "terminating": False}

    async def run():
        rollout = FakeAdapter(rollout_plan())
        restart = UncertainRestart(pod_plan(), fail_on_delete=True)
        async with Store.open(DSN) as store:
            await store.migrate()
            executor = await build_executor(
                store,
                rollout,
                FakePrometheus([200.0]),
                tmp_path,
                cleanup=cleanup,
                restart_adapter=restart,
            )
            proposal, _ = await seed_legacy_card(
                store,
                executor,
                cleanup,
                proposal_factory=restart_proposal,
                status=ProposalStatus.UNKNOWN,
            )
            receipt = await seed_legacy_attempt(
                store,
                proposal,
                status=AttemptStatus.UNKNOWN,
                lock=True,
                result={"stage": "apply", "error": "TimeoutError"},
            )
            assert restart.calls == []
            assert receipt.status == AttemptStatus.UNKNOWN
            assert project_action_result(receipt)["written"] is None
            reconciler = ActionReconciler(store, adapter=rollout, restart_adapter=restart)
            evidence = await reconciler.prepare_reconciliation(
                proposal.id,
                actor="tester",
                sender_stopped=True,
            )
            assert evidence["evidence"]["original_uid"] == restart._plan.uid
            assert "同一 UID" in evidence["evidence"]["residual_risk"]
            final = await reconciler.reconcile_action(
                proposal.id,
                preparation_id=evidence["preparation_id"],
                actor="tester",
                written=False,
                verified=False,
                note="原 Pod 仍在",
            )
            assert project_action_result(final)["written"] is False
            assert not await store.target_is_locked(_key(proposal))

    asyncio.run(run())


def test_stale_driver_cannot_abandon_a_new_card(tmp_path, cleanup):
    async def run():
        async with Store.open(DSN) as store:
            executor, proposal, runtime, _ = await _approved(store, tmp_path, cleanup)
            old_lease = executor._session_lease
            sessions = SessionStore(store)
            await sessions.release_lease(old_lease)
            new_lease = await sessions.acquire_main_lease(proposal.session_id, "new-driver")
            assert new_lease is not None
            with pytest.raises(LeaseLost):
                await executor.abandon(proposal.id, actor="old-driver", reason="已失租")
            executor._session_lease = None
            with pytest.raises(ActionError) as caught:
                await executor.abandon(proposal.id, actor="unscoped", reason="无主线")
            assert caught.value.code == "scope_required"
            assert (await store.get_proposal(proposal.id)).status is ProposalStatus.APPROVED
            executor._session_lease = new_lease
            await runtime.drive(proposal.id)
            assert (await store.get_attempt(proposal.id)).send_granted_at is not None
            with pytest.raises(ActionError) as caught:
                await executor.abandon(proposal.id, actor="new-driver", reason="已发送")
            assert caught.value.code == "already_claimed"
            assert runtime.kube.writes == [{"replicas": 2}]

    asyncio.run(run())


def test_new_approval_cannot_send_with_changed_policy_version(tmp_path, cleanup):
    async def run():
        async with Store.open(DSN) as store:
            _, proposal, runtime, _ = await _approved(store, tmp_path, cleanup)
            runtime.policy = runtime.policy.model_copy(update={"version": "changed"})
            await runtime.drive(proposal.id)
            assert runtime.kube.dry_runs == runtime.kube.writes == []
            assert await store.get_attempt(proposal.id) is None
            assert (await store.get_proposal(proposal.id)).status is ProposalStatus.FAILED
            assert not await store.target_is_locked(_key(proposal))

    asyncio.run(run())
