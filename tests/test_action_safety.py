"""动作权限、收尾及切换使用真实 PostgreSQL 与受控集群替身。"""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest
from actions_support import (
    DSN,
    FakeAdapter,
    FakePrometheus,
    FakeRestartAdapter,
    build_executor,
    make_proposal,
    pod_plan,
    rollout_plan,
)

from aiops.actions import executor as executor_module
from aiops.actions import safety as safety_module
from aiops.actions.kubernetes_write import WRITE_TIMEOUT_SECONDS
from aiops.actions.policy import decide, target_key
from aiops.actions.reconciliation import ActionReconciler, LegacyActionCutover
from aiops.actions.rollout_undo import ActionError
from aiops.actions.safety import ActionSafety
from aiops.contracts import ActionAttempt, ApprovalDecision, AttemptStatus, ProposalStatus
from aiops.db import Store
from aiops.sessions.models import LeaseLost
from aiops.sessions.projection import project_action_result, project_payload, project_template_diff
from aiops.sessions.store import SessionStore

pytestmark = pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL")


async def _new(store, tmp_path, cleanup, *, adapter=None, **kwargs):
    await store.migrate()
    adapter = adapter or FakeAdapter(rollout_plan())
    executor = await build_executor(
        store,
        adapter,
        FakePrometheus([200.0, 200.0]),
        tmp_path,
        cleanup=cleanup,
        **kwargs,
    )
    proposal, _, _ = await executor.prepare_rollback(
        deployment="productcatalogservice",
        reason="凭据 token=synthetic-key",
        actor="tester",
    )
    cleanup.append(proposal.id)
    return executor, proposal, adapter


async def _approved(store, tmp_path, cleanup, **kwargs):
    executor, proposal, adapter = await _new(store, tmp_path, cleanup, **kwargs)
    event = await executor.publish_test_card(proposal.id)
    approval = await ActionSafety(store).record_card_decision(
        proposal.id,
        session_id=proposal.session_id,
        fingerprint=event.payload["fingerprint"],
        decision=ApprovalDecision.APPROVED,
        actor="tester",
    )
    return executor, proposal, adapter, approval


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
        plan = replace(
            rollout_plan(),
            replicas=2,
            changes=[
                {"op": "remove", "path": "/spec/containers/0/extra", "current": "removed-value"},
                {
                    "op": "remove",
                    "path": "/spec/containers/0/env/0",
                    "current": {"name": "CREDENTIAL", "value": "original-private-value"},
                },
                {
                    "op": "add",
                    "path": "/spec/containers/0/env",
                    "value": [
                        {"name": "CUSTOM", "value": "opaque-fake-password"},
                    ],
                },
            ],
        )
        async with Store.open(DSN) as store:
            executor, proposal, adapter = await _new(
                store,
                tmp_path,
                cleanup,
                adapter=FakeAdapter(plan),
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
            assert card.payload["template_diff"][0]["op"] == "remove"
            assert card.payload["template_diff"][0]["current"] == "removed-value"
            assert card.payload["parameters"]["replicas"] == 2
            assert "original-private-value" not in json.dumps(card.payload)
            assert "opaque-fake-password" not in json.dumps(card.payload)
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
            assert adapter.calls == []

    asyncio.run(run())


def test_close_wins_after_intent_and_revokes_approval(tmp_path, cleanup, monkeypatch):
    async def run():
        ready, resume = asyncio.Event(), asyncio.Event()
        original = ActionSafety.grant_send

        async def pause(self, reservation, attempt, approval):
            ready.set()
            await resume.wait()
            return await original(self, reservation, attempt, approval)

        monkeypatch.setattr(ActionSafety, "grant_send", pause)
        async with Store.open(DSN) as driver, Store.open(DSN) as service:
            executor, proposal, adapter, _ = await _approved(
                driver,
                tmp_path,
                cleanup,
                group_key=f"alert-{uuid4().hex}",
            )
            sessions = SessionStore(service)
            session = await sessions.get_session(proposal.session_id)
            original_task = asyncio.create_task(executor.execute_authorized(proposal.id))
            await asyncio.wait_for(ready.wait(), 5)
            async with sessions.alert_transaction(session.group_key) as conn:
                event = await ActionSafety(service).close_session(conn, session.id, reason="已恢复")
                assert event.type == "session_closed"
            resume.set()
            with pytest.raises(ActionError):
                await original_task
            assert adapter.calls == [True]
            assert not await service.target_is_locked(_key(proposal))
            assert (await service.get_proposal(proposal.id)).status == ProposalStatus.VOIDED
            assert (await service.get_approval(proposal.id)).revoked_at is not None
            assert (await service.get_attempt(proposal.id)).status == AttemptStatus.VOIDED
            events = await sessions.list_events(session.id)
            assert [
                (event.payload["phase"], event.payload["written"])
                for event in events
                if event.type == "action_result"
            ] == [("intent", False), ("intent", False)]

    asyncio.run(run())


@pytest.mark.parametrize("close", [False, True])
def test_live_verifier_renews_after_session_takeover_or_close(
    tmp_path, cleanup, monkeypatch, close
):
    # 缩短时限，让验证跨过首代锁的有期窗口。
    monkeypatch.setattr(safety_module, "ACTION_LOCK_SECONDS", 2)
    monkeypatch.setattr(executor_module, "ACTION_HEARTBEAT_SECONDS", 0.15)
    verifying, complete = Event(), Event()

    class SlowVerification(FakeAdapter):
        def wait_rolled_out(self, plan, timeout_seconds):
            verifying.set()
            return complete.wait(8)

    async def run():
        async with Store.open(DSN) as driver, Store.open(DSN) as service:
            executor, proposal, adapter, _ = await _approved(
                driver,
                tmp_path,
                cleanup,
                adapter=SlowVerification(rollout_plan()),
            )
            original_task = asyncio.create_task(executor.execute_authorized(proposal.id))
            successor_task = None
            assert await asyncio.to_thread(verifying.wait, 5)
            try:
                sessions = SessionStore(service)
                if close:
                    async with service.transaction() as conn:
                        await ActionSafety(service).close_session(
                            conn, proposal.session_id, reason="关闭"
                        )
                else:
                    await sessions.release_lease(executor._session_lease)
                    successor = await build_executor(
                        service,
                        adapter,
                        FakePrometheus([200.0, 200.0]),
                        tmp_path,
                        cleanup=cleanup,
                    )
                    successor_task = asyncio.create_task(successor.resume(proposal.id))
                await asyncio.sleep(2.2)
                await ActionSafety(service).recover_target(_key(proposal))
                assert await service.target_is_locked(_key(proposal))
                assert (await service.get_attempt(proposal.id)).status == AttemptStatus.EXECUTING
                with pytest.raises(ActionError, match="原发送者"):
                    await ActionReconciler(service, adapter=adapter).prepare_reconciliation(
                        proposal.id,
                        actor="tester",
                    )
                if successor_task:
                    assert not successor_task.done()
            finally:
                complete.set()
            result = await original_task
            if successor_task:
                assert await successor_task == result
            assert result.status == AttemptStatus.VERIFIED
            assert adapter.calls == [True, False]
            assert not await service.target_is_locked(_key(proposal))
            assert project_action_result(result)["written"] is True
            rollbacks = await ActionSafety(service).list_verified_rollbacks(
                since=datetime.now(UTC) - timedelta(minutes=1),
                cluster=proposal.target.cluster,
            )
            row = next(item for item in rollbacks if item["card_id"] == proposal.id)
            assert row["approved_by"] == "tester"
            assert row["target_revision"] == "1"

    asyncio.run(run())


class SimulatedProcessExit(BaseException):
    pass


@pytest.mark.parametrize("crash_after", ["reserve", "save_intent", "grant_send"])
def test_recovery_distinguishes_lock_intent_and_send_grant(
    tmp_path, cleanup, monkeypatch, crash_after
):
    original = getattr(ActionSafety, crash_after)

    async def crash(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        raise SimulatedProcessExit()

    monkeypatch.setattr(ActionSafety, crash_after, crash)

    async def run():
        async with Store.open(DSN) as driver, Store.open(DSN) as recovery:
            executor, proposal, adapter, _ = await _approved(driver, tmp_path, cleanup)
            with pytest.raises(SimulatedProcessExit):
                await executor.execute_authorized(proposal.id)
            assert adapter.calls.count(False) == 0
            await _expire(recovery, proposal)
            lease = await SessionStore(recovery).acquire_main_lease(proposal.session_id, "recovery")
            executor._session_lease = lease
            if crash_after == "reserve":
                await ActionSafety(recovery).recover_target(_key(proposal))
            attempt = await executor.read_action_attempt(proposal.id)
            saved = await recovery.get_proposal(proposal.id)
            if crash_after == "grant_send":
                assert saved.status == ProposalStatus.UNKNOWN
                assert await recovery.target_is_locked(_key(proposal))
                assert attempt.status == AttemptStatus.UNKNOWN
                assert project_action_result(attempt)["written"] is None
                monkeypatch.setattr(ActionSafety, crash_after, original)
                assert await executor.execute_authorized(proposal.id) == attempt
            else:
                assert saved.status == ProposalStatus.VOIDED
                assert not await recovery.target_is_locked(_key(proposal))
                assert attempt is None or attempt.status == AttemptStatus.VOIDED
                assert (await recovery.get_approval(proposal.id)).revoked_at is not None
            assert adapter.calls.count(False) == 0

    asyncio.run(run())


def test_human_verification_fences_late_rollback_and_keeps_lock_on_failure(tmp_path, cleanup):
    class LostResponse(FakeAdapter):
        version = "10"
        fence = None
        fail_isolation = True
        sends = 0

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
            return self.version

        def state(self, namespace, name):
            return {
                "uid": self._plan.deployment_uid,
                "resource_version": self.version,
                "reconciliation_marker": self.fence,
                "token": "fake-token",
            }

    async def run():
        adapter = LostResponse(rollout_plan())
        async with Store.open(DSN) as store:
            executor, proposal, _, _ = await _approved(store, tmp_path, cleanup, adapter=adapter)
            receipt = await executor.execute_authorized(proposal.id)
            assert adapter.sends == 1 and receipt.status == AttemptStatus.UNKNOWN
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
            await store.save_proposal(pending, decide(pending, target_busy=False))
            await store.save_proposal(unknown, decide(unknown, target_busy=False))
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
            await store.save_proposal(proposal, decide(proposal, target_busy=False))
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
            proposal, _, _ = await executor.prepare_restart_pod(
                pod=restart._plan.name,
                service="rebuild-demo",
                path="/work",
                reason="重建",
                actor="tester",
            )
            cleanup.append(proposal.id)
            receipt = await executor.approve(proposal.id, actor="tester")
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


def test_stale_and_unscoped_drivers_cannot_abandon_or_execute(tmp_path, cleanup):
    async def run():
        async with Store.open(DSN) as store:
            executor, proposal, adapter, _ = await _approved(store, tmp_path, cleanup)
            old_lease = executor._session_lease
            await SessionStore(store).release_lease(old_lease)
            new_lease = await SessionStore(store).acquire_main_lease(
                proposal.session_id, "new-driver"
            )
            with pytest.raises(LeaseLost):
                await executor.abandon(proposal.id, actor="old-driver", reason="已失租")
            with pytest.raises(LeaseLost):
                await executor.execute_authorized(proposal.id)
            executor._session_lease = None
            with pytest.raises(ActionError) as caught:
                await executor.execute_authorized(proposal.id)
            assert caught.value.code == "scope_required"
            assert (await store.get_proposal(proposal.id)).status == ProposalStatus.APPROVED
            assert adapter.calls == []
            executor._session_lease = new_lease
            attempt = await executor.execute_authorized(proposal.id)
            assert attempt.send_granted_at is not None
            with pytest.raises(ActionError) as caught:
                await executor.abandon(proposal.id, actor="new-driver", reason="已发送")
            assert caught.value.code == "already_claimed"

    asyncio.run(run())


def test_approval_binds_verification_settings_across_drivers(tmp_path, cleanup):
    async def run():
        async with Store.open(DSN) as store:
            executor, proposal, adapter, _ = await _approved(store, tmp_path, cleanup)
            executor._probe_expression = "changed_probe"
            with pytest.raises(ActionError) as caught:
                await executor.execute_authorized(proposal.id)
            assert caught.value.code == "verification_changed"
            assert adapter.calls == []
            assert (await store.get_proposal(proposal.id)).status == ProposalStatus.FAILED

    asyncio.run(run())
