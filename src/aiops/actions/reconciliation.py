"""人工核实和旧库切换，保持结果不明的目标锁。"""

import asyncio
from datetime import timedelta
from typing import Any
from uuid import uuid4

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from aiops.actions.kubernetes_write import WRITE_TIMEOUT_SECONDS
from aiops.actions.restart_pod import PodRestartAdapter
from aiops.actions.rollout_undo import ActionError, RolloutUndoAdapter
from aiops.actions.safety import OPEN_UNSENT, POSSIBLY_SENT, ActionSafety, _key, _now, _one
from aiops.contracts import ActionAttempt, ActionKind
from aiops.db import Store
from aiops.sessions.projection import project_action_result, project_payload, project_text


def _legacy_classification(row: dict) -> str:
    status = row["status"]
    attempt = row["attempt_status"]
    if status in POSSIBLY_SENT and attempt not in POSSIBLY_SENT:
        # 旧实现先更新提案为 executing，再更新 attempt 为 executing。
        if status == "executing" and attempt == "intent":
            return "void"
        return "unclear"
    if attempt in POSSIBLY_SENT:
        return "retain"
    if attempt == "intent":
        return "void" if status in OPEN_UNSENT else "unclear"
    if status in OPEN_UNSENT:
        return "void" if attempt is None else "unclear"
    if status == "voided" and attempt not in {None, "voided"}:
        return "unclear"
    return "keep"


class ActionReconciler:
    def __init__(
        self,
        store: Store,
        *,
        adapter: RolloutUndoAdapter,
        restart_adapter: PodRestartAdapter | None = None,
    ) -> None:
        self.safety = ActionSafety(store)
        self.store = store
        self.adapter = adapter
        self.restart_adapter = restart_adapter

    async def get_reconciliation(self, proposal_id: str) -> dict[str, Any] | None:
        """查询脱敏后的处置、原发送者与已保存的核实依据。"""
        async with self.store.transaction() as conn:
            row = await _one(
                conn,
                """
                SELECT p.*, a.id AS attempt_id, a.status AS attempt_status, a.send_granted_at,
                       a.reconciliation, a.request_timeout_seconds,
                       l.owner, l.generation, l.expires_at
                FROM action_proposals p
                JOIN action_attempts a ON a.proposal_id = p.id
                LEFT JOIN action_locks l ON l.proposal_id = p.id
                WHERE p.id = %s
                  AND (a.status IN ('executing', 'unknown') OR a.reconciliation IS NOT NULL)
                """,
                (proposal_id,),
            )
            if row is None:
                return None
            return project_payload(
                {
                    "card_id": proposal_id,
                    "action": row["kind"],
                    "status": row["attempt_status"],
                    "target": {
                        key: row[key]
                        for key in (
                            "cluster",
                            "namespace",
                            "target_kind",
                            "target_name",
                            "target_uid",
                        )
                    },
                    "attempt_id": row["attempt_id"],
                    "send_granted_at": row["send_granted_at"].isoformat()
                    if row["send_granted_at"]
                    else None,
                    "owner": row["owner"],
                    "generation": row["generation"],
                    "lease_expires_at": row["expires_at"].isoformat()
                    if row["expires_at"]
                    else None,
                    "request_timeout_seconds": row["request_timeout_seconds"],
                    "reconciliation": row["reconciliation"],
                }
            )

    async def prepare_reconciliation(
        self,
        proposal_id: str,
        *,
        actor: str,
        sender_stopped: bool = False,
    ) -> dict[str, Any]:
        """隔离后取证。操作者读此凭据才可提交结论，期间保留目标锁。"""
        if not actor.strip():
            raise ActionError("actor_missing", "核实需要操作者")
        async with self.store.transaction() as conn:
            row, attempt, lock = await self._records(conn, proposal_id)
            await self._require_quiet(conn, attempt, lock, sender_stopped=sender_stopped)
            # 一旦确认可以开始核实，原尝试也不能再覆盖最终结果。
            if attempt["status"] == "executing":
                await self.safety.mark_unknown(conn, row, attempt)
        async with self.store.transaction() as conn:
            row, attempt, lock = await self._records(conn, proposal_id)
            await self._require_quiet(conn, attempt, lock, sender_stopped=sender_stopped)
            if attempt["status"] != "unknown":
                raise ActionError("not_unknown", "动作无需人工核实")
            snapshot = attempt["snapshot"]
            uid = snapshot.get("uid") or row["target_uid"]
            if not uid or uid != row["target_uid"]:
                raise ActionError("identity_unknown", "无法确认批准目标身份")
            preparation = uuid4().hex
            namespace, name = row["namespace"], row["target_name"]
            if row["kind"] == ActionKind.ROLLOUT_UNDO:
                version = snapshot.get("resource_version") or row["target_resource_version"]
                if not version:
                    raise ActionError("version_unknown", "无法确认原回滚请求的版本条件")
                isolated = await asyncio.to_thread(
                    self.adapter.isolate,
                    namespace,
                    name,
                    uid,
                    version,
                    preparation,
                )
                if not isolated or isolated == version:
                    raise ActionError("isolation_failed", "未确认原版本已失效")
                state = await asyncio.to_thread(self.adapter.state, namespace, name)
                if (
                    state.get("uid") != uid
                    or not state.get("resource_version")
                    or state["resource_version"] == version
                    or state.get("reconciliation_marker") != preparation
                ):
                    raise ActionError("isolation_failed", "隔离后无法核对 Deployment 身份与版本")
                evidence = {"isolated_version": isolated, "state": state}
            elif row["kind"] == ActionKind.RESTART_POD and self.restart_adapter is not None:
                state = await asyncio.to_thread(
                    self.restart_adapter.reconciliation_state,
                    namespace,
                    name,
                    uid,
                )
                if not isinstance(state.get("original_present"), bool):
                    raise ActionError("identity_unknown", "无法核对原 Pod UID")
                evidence = {
                    "state": state,
                    "original_uid": uid,
                    "residual_risk": "迟到的请求仍可能删除同一 UID 的原 Pod",
                }
            else:
                raise ActionError("adapter_missing", "未配置适用的核实适配器")
            prepared = {
                "preparation_id": preparation,
                "evidence": project_payload(evidence),
                "prepared_at": (await _now(conn)).isoformat(),
            }
            await conn.execute(
                "UPDATE action_attempts SET reconciliation = %s "
                "WHERE id = %s AND status = 'unknown'",
                (Jsonb(prepared), attempt["id"]),
            )
            await self.safety.audit(
                conn,
                proposal_id,
                actor,
                "action.reconciliation_prepared",
                {"attempt_id": attempt["id"], "sender_stopped": sender_stopped, **prepared},
            )
            return prepared

    async def reconcile_action(
        self,
        proposal_id: str,
        *,
        preparation_id: str,
        actor: str,
        written: bool | None,
        verified: bool,
        note: str,
    ) -> ActionAttempt:
        """一次提交人工结论、提案终态、待办解决与解锁。"""
        if not actor.strip() or not note.strip():
            raise ActionError("note_required", "结论需要操作者和核实依据")
        if (
            not isinstance(written, bool)
            or not isinstance(verified, bool)
            or (verified and not written)
        ):
            raise ActionError("result_unknown", "写入结果不明，保留目标锁")
        async with self.store.transaction() as conn:
            row, attempt, _ = await self._records(conn, proposal_id)
            prepared = attempt["reconciliation"]
            if not prepared or prepared["preparation_id"] != preparation_id:
                raise ActionError("preparation_changed", "核实凭据已改变，需重新核实")
            evidence = prepared["evidence"]
            if row["kind"] == ActionKind.ROLLOUT_UNDO:
                state = await asyncio.to_thread(
                    self.adapter.state, row["namespace"], row["target_name"]
                )
                if (
                    state.get("uid") != row["target_uid"]
                    or state.get("resource_version") != evidence["state"]["resource_version"]
                    or state.get("reconciliation_marker") != preparation_id
                ):
                    raise ActionError("state_changed", "目标状态已变化，需重新核实")
            else:
                state = await asyncio.to_thread(
                    self.restart_adapter.reconciliation_state,
                    row["namespace"],
                    row["target_name"],
                    row["target_uid"],
                )
                if state != evidence["state"]:
                    raise ActionError("state_changed", "Pod 状态已变化，需重新核实")
            receipt = await self.safety.finish_locked(
                conn,
                row,
                attempt,
                "verified" if verified else "failed",
                "succeeded" if verified else "failed",
                result={
                    "stage": "manual_verification",
                    "written": written,
                },
                verification={
                    "ok": verified,
                    "actor": project_text(actor),
                    "note": project_text(note),
                    **evidence,
                },
                followup=None,
            )
            await conn.execute(
                """
                UPDATE action_followups
                SET status = 'resolved', resolved_at = clock_timestamp(),
                    resolved_by = %s, resolution = %s
                WHERE attempt_id = %s AND status = 'open'
                """,
                (actor, note, attempt["id"]),
            )
            await self.safety.audit(
                conn,
                proposal_id,
                actor,
                "action.reconciled",
                {"preparation_id": preparation_id, "note": note, **project_action_result(receipt)},
            )
            return receipt

    async def _records(self, conn: AsyncConnection, proposal_id: str) -> tuple[dict, dict, dict]:
        proposal = await _one(
            conn,
            "SELECT session_id FROM action_proposals WHERE id = %s",
            (proposal_id,),
        )
        if proposal is None:
            raise ActionError("not_found", "提案不存在")
        if proposal["session_id"] is not None:
            await self.safety.sessions.lock_session(conn, proposal["session_id"])
        row = await self.safety._proposal(conn, proposal_id)
        attempt = await self.safety._attempt(conn, proposal_id)
        lock = await self.safety._lock(conn, _key(row))
        if (
            attempt is None
            or attempt["status"] not in POSSIBLY_SENT
            or lock is None
            or lock["proposal_id"] != proposal_id
        ):
            raise ActionError("not_reconcilable", "未发现仍锁定的待核实动作")
        return row, attempt, lock

    async def _require_quiet(
        self,
        conn: AsyncConnection,
        attempt: dict,
        lock: dict,
        *,
        sender_stopped: bool,
    ) -> None:
        if lock["owner"] is None:
            if (attempt["result"] or {}).get("stage") != "legacy_cutover":
                raise ActionError("legacy_cutover_required", "先完成旧库切换准备")
            return
        wait_until = lock["expires_at"] + timedelta(
            seconds=attempt["request_timeout_seconds"] or WRITE_TIMEOUT_SECONDS
        )
        if sender_stopped is not True and await _now(conn) <= wait_until:
            raise ActionError("sender_live", "原发送者仍可能在运行或仍有在途请求")


class LegacyActionCutover:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.safety = ActionSafety(store)

    async def preview(self) -> dict[str, Any]:
        async with self.store.transaction() as conn:
            return project_payload(await self._plan(conn))

    async def prepare(
        self,
        *,
        actor: str,
        confirmed: bool,
        old_processes_stopped: bool,
        restart_disabled: bool,
        backup_complete: bool,
    ) -> dict[str, Any]:
        if not (
            actor.strip()
            and confirmed
            and old_processes_stopped
            and restart_disabled
            and backup_complete
        ):
            raise ActionError("confirmation_required", "须确认旧进程已停止、禁用重启且数据库已备份")
        async with self.store.transaction() as conn:
            # 固定遗留集的分类顺序，重复执行只保留已经处置的记录。
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext('vigil:legacy_action_cutover'))"
            )
            plan = await self._plan(conn)
            if plan["blocked"]:
                raise ActionError(
                    "legacy_cutover_blocked", "存在有效旧租约、孤立锁或未能分类的动作"
                )
            for item in plan["actions"]:
                row = await self.safety._proposal(conn, item["card_id"])
                attempt = await self.safety._attempt(conn, row["id"])
                lock = await self.safety._lock(conn, _key(row))
                classification = _legacy_classification(
                    {
                        **row,
                        "attempt_status": attempt["status"] if attempt else None,
                    }
                )
                if classification == "void":
                    await self.safety._void(
                        conn, row, attempt, actor=actor, reason="legacy_cutover"
                    )
                elif classification == "retain":
                    if lock is None:
                        await conn.execute(
                            "INSERT INTO action_locks (target_key, proposal_id) VALUES (%s, %s)",
                            (_key(row), row["id"]),
                        )
                    elif lock["proposal_id"] != row["id"] or lock["owner"] is not None:
                        raise ActionError("legacy_target_conflict", "目标锁属于另一项处置")
                    if (attempt["result"] or {}).get("stage") != "legacy_cutover":
                        await self.safety.mark_unknown(conn, row, attempt)
                        await conn.execute(
                            """
                            UPDATE action_attempts SET result = coalesce(result, '{}'::jsonb) || %s
                            WHERE id = %s
                            """,
                            (Jsonb({"stage": "legacy_cutover"}), attempt["id"]),
                        )
                        await self.safety.audit(
                            conn,
                            row["id"],
                            actor,
                            "action.legacy_cutover",
                            {"outcome": "retain"},
                        )
                elif classification == "keep":
                    await self.safety._unlock(conn, row)
                else:
                    raise ActionError("legacy_cutover_blocked", "动作状态在执行切换前改变")
            return project_payload(plan)

    async def _plan(self, conn: AsyncConnection) -> dict[str, Any]:
        leases = await conn.execute("""
            SELECT t.investigation_id, t.lease_owner, t.lease_expires_at,
                   t.lease_expires_at > clock_timestamp() AS active
            FROM investigation_tasks t JOIN investigations i ON i.id = t.investigation_id
            WHERE i.session_turn_id IS NULL
              AND (t.lease_owner IS NOT NULL OR t.lease_expires_at IS NOT NULL)
            ORDER BY t.investigation_id
            """)
        leases = [
            {
                **row,
                "lease_expires_at": row["lease_expires_at"].isoformat()
                if row["lease_expires_at"]
                else None,
            }
            for row in await leases.fetchall()
        ]
        rows = await conn.execute("""
            SELECT p.*, a.status AS attempt_status, a.id AS attempt_id
            FROM action_proposals p LEFT JOIN action_attempts a ON a.proposal_id = p.id
            WHERE p.session_id IS NULL AND (
                p.status IN ('pending_approval', 'approved', 'executing', 'unknown')
                OR a.status IN ('intent', 'executing', 'unknown')
                OR EXISTS (SELECT 1 FROM action_locks l WHERE l.proposal_id = p.id)
            ) ORDER BY p.id
            """)
        actions = [
            {
                "card_id": row["id"],
                "attempt_id": row["attempt_id"],
                "target": _key(row),
                "proposal_status": row["status"],
                "attempt_status": row["attempt_status"],
                "classification": _legacy_classification(row),
            }
            for row in await rows.fetchall()
        ]
        orphans = await conn.execute("""
            SELECT l.target_key, l.proposal_id FROM action_locks l
            LEFT JOIN action_proposals p ON p.id = l.proposal_id
            WHERE p.id IS NULL OR (
                p.session_id IS NULL AND l.target_key <>
                    concat_ws('/', p.cluster, p.namespace, p.target_kind, p.target_name)
            ) ORDER BY l.target_key
            """)
        orphans = await orphans.fetchall()
        return project_payload(
            {
                "leases": leases,
                "actions": actions,
                "unmatched_locks": orphans,
                "blocked": bool(
                    orphans
                    or any(row["classification"] == "unclear" for row in actions)
                    or any(row["active"] is not False or not row["lease_owner"] for row in leases)
                ),
            }
        )
