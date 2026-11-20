"""卡片决定、发送权及处置事务。加锁顺序为会话、提案、尝试、目标锁。"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from aiops.actions.kubernetes_write import WRITE_TIMEOUT_SECONDS
from aiops.actions.policy import AUTO_NAMESPACES, target_key
from aiops.actions.rollout_undo import ActionError
from aiops.contracts import (
    ActionAttempt,
    ActionFollowup,
    ActionProposal,
    Approval,
    ApprovalDecision,
    AttemptOutcome,
    AttemptStatus,
    ProposalStatus,
    RiskDecision,
)
from aiops.db import Store, _approval_from_row, _attempt_from_row, _proposal_from_row
from aiops.sessions.checkpoint import TURN_ID_METADATA
from aiops.sessions.models import Lease, LeaseLost, SessionEvent
from aiops.sessions.projection import (
    card_url,
    project_action_result,
    project_payload,
    project_template_diff,
    project_text,
)
from aiops.sessions.store import SessionStore

ACTION_LOCK_SECONDS = 120
ACTION_HEARTBEAT_SECONDS = 30
POSSIBLY_SENT = {"executing", "unknown"}
OPEN_UNSENT = {"pending_approval", "approved"}


def proposal_hash(proposal: ActionProposal) -> str:
    payload = proposal.model_dump(mode="json", exclude={"status", "created_at"})
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _key(row: dict[str, Any]) -> str:
    return target_key(row["cluster"], row["namespace"], row["target_kind"], row["target_name"])


async def _one(conn: AsyncConnection, query: str, params: tuple = ()) -> dict | None:
    return await (await conn.execute(query, params)).fetchone()


async def _now(conn: AsyncConnection):
    return (await _one(conn, "SELECT clock_timestamp() AS now"))["now"]


@dataclass(frozen=True)
class Reservation:
    proposal_id: str
    key: str
    owner: str
    generation: int
    lease: Lease
    turn_id: str


class ActionSafety:
    def __init__(
        self,
        store: Store,
        *,
        web_url: str | None = None,
        approval_ttl_seconds: int = 900,
    ) -> None:
        self.store = store
        self.sessions = SessionStore(store)
        self.web_url = web_url
        self.approval_ttl = approval_ttl_seconds

    async def save(
        self,
        proposal: ActionProposal,
        risk: RiskDecision,
        *,
        lease: Lease,
        turn_id: str,
        auto_actor: str | None,
    ) -> Approval | None:
        async with self.store.transaction() as conn:
            await self.sessions.require_lease(conn, lease, main_only=True)
            await self.sessions.require_turn(conn, lease, turn_id)
            if (proposal.session_id, proposal.session_turn_id) != (lease.session_id, turn_id):
                raise LeaseLost(turn_id)
            if proposal.investigation_id:
                scoped = await _one(
                    conn,
                    "SELECT session_turn_id FROM investigations WHERE id = %s",
                    (proposal.investigation_id,),
                )
                if scoped is None or scoped["session_turn_id"] != turn_id:
                    raise LeaseLost(turn_id)
            await self.store._insert_proposal(conn, proposal, risk)
            approval = None
            if auto_actor is not None:
                approval = await self._insert_decision(
                    conn,
                    proposal,
                    ApprovalDecision.APPROVED,
                    auto_actor,
                    "自动允许：" + "；".join(risk.reasons),
                )
                await self.audit(
                    conn,
                    proposal.id,
                    auto_actor,
                    "action.auto_approved",
                    {"approval_id": approval.id, "policy_version": risk.policy_version},
                )
            await self.sessions.require_lease(conn, lease, main_only=True)
            return approval

    async def _insert_decision(
        self,
        conn: AsyncConnection,
        proposal: ActionProposal,
        decision: ApprovalDecision,
        actor: str,
        reason: str,
    ) -> Approval:
        now = await _now(conn)
        approval = Approval(
            id=f"apr-{uuid4().hex}",
            proposal_id=proposal.id,
            proposal_hash=proposal_hash(proposal),
            actor=actor,
            decision=decision,
            reason=reason,
            decided_at=now,
            expires_at=now + timedelta(seconds=self.approval_ttl)
            if decision is ApprovalDecision.APPROVED
            else None,
        )
        await self.store._insert_approval(conn, approval)
        return approval

    async def _card(self, conn: AsyncConnection, row: dict) -> dict[str, Any]:
        proposal = _proposal_from_row(row)
        parameters = proposal.parameters
        refs = await conn.execute(
            """
            SELECT id, observation_id FROM hypothesis_evidence
            WHERE investigation_id = %s AND hypothesis_id = %s AND observation_id = ANY(%s)
            """,
            (proposal.investigation_id, proposal.hypothesis_id, proposal.evidence_ids),
        )
        by_observation = {ref["observation_id"]: ref["id"] for ref in await refs.fetchall()}
        return {
            "card_id": proposal.id,
            "action": proposal.kind.value,
            "target": project_payload(proposal.target.model_dump(mode="json")),
            "risk": {
                "disposition": row["risk_disposition"],
                "reasons": project_payload(row["risk_reasons"]),
                "policy_version": row["policy_version"],
            },
            "url": card_url(self.web_url, row["session_id"], proposal.id),
            "status": proposal.status.value,
            "fingerprint": proposal_hash(proposal),
            "evidence_ids": [by_observation.get(obs, obs) for obs in proposal.evidence_ids],
            "reason": project_text(proposal.reason),
            "parameters": project_payload(
                {
                    key: parameters[key]
                    for key in (
                        "from_revision",
                        "to_revision",
                        "replicas",
                        "target_image",
                        "from_memory_limit",
                        "to_memory_limit",
                        "replica_set",
                        "pod",
                        "node_name",
                        "restart_count",
                        "last_exit_reason",
                        "service",
                        "path",
                    )
                    if key in parameters
                }
            ),
            "template_diff": project_template_diff(parameters.get("template_diff") or []),
            "verification": project_payload(proposal.observation_conditions),
            "purpose": proposal.purpose.value,
        }

    async def publish_card(self, proposal_id: str, *, lease: Lease, turn_id: str) -> SessionEvent:
        async with self.store.transaction() as conn:
            await self.sessions.require_lease(conn, lease, main_only=True)
            turn = await self.sessions.require_turn(conn, lease, turn_id)
            row = await self._proposal(conn, proposal_id)
            self._check_scope(row, lease, turn_id)
            if row["published_seq"] is not None:
                event = await _one(
                    conn,
                    "SELECT * FROM session_events WHERE session_id = %s AND seq = %s",
                    (lease.session_id, row["published_seq"]),
                )
                return SessionEvent(**event)
            if row["status"] != "pending_approval" or row["risk_disposition"] != "approval":
                raise ActionError("not_pending", "卡片不在待审批状态")
            # 只有最近的根 checkpoint 已持久化中断后，才开放审批。
            checkpoint = await _one(
                conn,
                """
                SELECT c.checkpoint_id FROM checkpoints c
                WHERE c.thread_id = %s AND c.checkpoint_ns = ''
                  AND c.metadata ->> %s = %s
                  AND c.checkpoint_id = (
                      SELECT max(latest.checkpoint_id) FROM checkpoints latest
                      WHERE latest.thread_id = c.thread_id AND latest.checkpoint_ns = ''
                  )
                  AND EXISTS (
                      SELECT 1 FROM checkpoint_writes w
                      WHERE w.thread_id = c.thread_id AND w.checkpoint_ns = c.checkpoint_ns
                        AND w.checkpoint_id = c.checkpoint_id AND w.channel = '__interrupt__'
                  )
                ORDER BY c.checkpoint_id DESC LIMIT 1
                """,
                (turn.checkpoint_thread_id, TURN_ID_METADATA, turn_id),
            )
            if checkpoint is None:
                raise ActionError("checkpoint_missing", "等待审批的 checkpoint 尚未持久化")
            event = await self.sessions.append_event(
                conn,
                lease.session_id,
                "approval_required",
                await self._card(conn, row),
                turn_id=turn_id,
            )
            await conn.execute(
                "UPDATE action_proposals SET published_seq = %s WHERE id = %s",
                (event.seq, proposal_id),
            )
            await self.sessions.require_lease(conn, lease, main_only=True)
            return event

    async def get_card(self, proposal_id: str, *, session_id: str) -> dict[str, Any] | None:
        async with self.store.transaction() as conn:
            row = await _one(
                conn,
                """
                SELECT p.status, e.payload
                FROM action_proposals p JOIN session_events e
                  ON e.session_id = p.session_id AND e.seq = p.published_seq
                WHERE p.id = %s AND p.session_id = %s
                """,
                (proposal_id, session_id),
            )
            return {**row["payload"], "status": row["status"]} if row else None

    async def list_runnable_card_sessions(self, *, limit: int = 50) -> list[str]:
        async with self.store.transaction() as conn:
            rows = await conn.execute(
                """
                SELECT s.id FROM sessions s JOIN session_turns t ON t.session_id = s.id
                WHERE t.lane = 'main' AND t.state = 'awaiting_approval'
                  AND s.closed_at IS NULL
                  AND (s.lease_expires_at IS NULL OR s.lease_expires_at <= clock_timestamp())
                  AND (
                      SELECT p.status FROM action_proposals p
                      WHERE p.session_id = s.id AND p.session_turn_id = t.id
                        AND p.published_seq IS NOT NULL
                      ORDER BY p.published_seq DESC LIMIT 1
                  ) <> 'pending_approval'
                ORDER BY s.updated_at, s.id LIMIT %s
                """,
                (limit,),
            )
            return [row["id"] for row in await rows.fetchall()]

    async def pending_card_sessions(self, session_ids: list[str]) -> set[str]:
        if not session_ids:
            return set()
        async with self.store.transaction() as conn:
            rows = await conn.execute(
                """
                SELECT DISTINCT p.session_id FROM action_proposals p
                JOIN sessions s ON s.id = p.session_id
                WHERE p.session_id = ANY(%s) AND s.closed_at IS NULL
                  AND p.published_seq IS NOT NULL AND p.status = 'pending_approval'
                """,
                (session_ids,),
            )
            return {row["session_id"] for row in await rows.fetchall()}

    async def list_verified_rollbacks(
        self,
        *,
        since: datetime,
        cluster: str,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """提供监控面板和发布指标按同一个回滚记录关联 ReplicaSet 历史。"""
        async with self.store.transaction() as conn:
            rows = await conn.execute(
                """
                SELECT p.id AS card_id, p.namespace, p.target_name AS deployment,
                       p.target_uid AS deployment_uid,
                       p.parameters->>'to_revision' AS target_revision,
                       p.parameters->'replica_set'->>'uid' AS replica_set_uid,
                       p.parameters->>'target_image' AS target_image,
                       coalesce(a.send_granted_at, a.started_at) AS changed_at,
                       a.finished_at, d.actor AS approved_by
                FROM action_attempts a JOIN action_proposals p ON p.id = a.proposal_id
                LEFT JOIN LATERAL (
                    SELECT actor FROM approvals
                    WHERE proposal_id = p.id AND decision = 'approved'
                      AND proposal_hash = a.snapshot->>'approval_hash'
                    ORDER BY decided_at DESC, id DESC LIMIT 1
                ) d ON true
                WHERE p.kind = 'rollout_undo' AND p.status = 'succeeded'
                  AND a.status = 'verified' AND p.cluster = %s
                  AND coalesce(a.send_granted_at, a.started_at) >= %s
                ORDER BY changed_at DESC, p.id LIMIT %s
                """,
                (cluster, since, limit),
            )
            return [
                project_payload(
                    {
                        **row,
                        "changed_at": row["changed_at"].isoformat(),
                        "finished_at": row["finished_at"].isoformat()
                        if row["finished_at"]
                        else None,
                    }
                )
                for row in await rows.fetchall()
            ]

    async def record_card_decision(
        self,
        proposal_id: str,
        *,
        session_id: str,
        fingerprint: str,
        decision: ApprovalDecision,
        actor: str,
        reason: str = "",
    ) -> Approval:
        decision = ApprovalDecision(decision)
        if not actor.strip():
            raise ActionError("actor_missing", "决定需要操作者")
        async with self.store.transaction() as conn:
            session = await self.sessions.lock_session(conn, session_id)
            if session.closed_at is not None:
                raise ActionError("session_closed", "会话已关闭")
            row = await self._proposal(conn, proposal_id, session_id=session_id)
            if (
                row["published_seq"] is None
                or row["status"] != "pending_approval"
                or row["risk_disposition"] != "approval"
            ):
                raise ActionError("not_pending", "卡片不在待审批状态")
            active = await _one(
                conn,
                "SELECT 1 FROM session_turns WHERE id = %s AND finished_at IS NULL",
                (row["session_turn_id"],),
            )
            if active is None:
                raise ActionError("not_pending", "卡片所属轮次已结束")
            proposal = _proposal_from_row(row)
            published = await _one(
                conn,
                "SELECT payload FROM session_events WHERE session_id = %s AND seq = %s",
                (session_id, row["published_seq"]),
            )
            if (
                not hmac.compare_digest(fingerprint.encode(), proposal_hash(proposal).encode())
                or published["payload"].get("fingerprint") != fingerprint
            ):
                raise ActionError("hash_mismatch", "卡片内容已变化，请刷新后核对")
            approval = await self._insert_decision(conn, proposal, decision, actor, reason)
            await self._status(conn, proposal.id, decision.value)
            await self.audit(
                conn,
                proposal.id,
                actor,
                f"action.{decision.value}",
                {"approval_id": approval.id, "fingerprint": approval.proposal_hash},
            )
            await self.sessions.append_event(
                conn,
                session_id,
                "card_decision",
                {
                    "card_id": proposal.id,
                    "status": decision.value,
                    "decision_id": approval.id,
                    "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
                    "actor": project_text(actor),
                    "reason": project_text(reason),
                },
                turn_id=row["session_turn_id"],
            )
            return approval

    async def get_card_decision(self, proposal_id: str, *, session_id: str) -> Approval | None:
        async with self.store.transaction() as conn:
            row = await _one(
                conn,
                """
                SELECT a.* FROM approvals a JOIN action_proposals p ON p.id = a.proposal_id
                WHERE p.id = %s AND p.session_id = %s AND p.published_seq IS NOT NULL
                  AND a.revoked_at IS NULL
                ORDER BY a.decided_at DESC, a.id DESC LIMIT 1
                """,
                (proposal_id, session_id),
            )
            return _approval_from_row(row) if row else None

    async def close_session(
        self,
        conn: AsyncConnection,
        session_id: str,
        *,
        reason: str,
    ) -> SessionEvent | None:
        event = await self.sessions.close_session(conn, session_id, reason=project_text(reason))
        rows = await conn.execute(
            """
            SELECT * FROM action_proposals WHERE session_id = %s
              AND status IN ('pending_approval', 'approved') ORDER BY id FOR UPDATE
            """,
            (session_id,),
        )
        for row in await rows.fetchall():
            attempt = await self._attempt(conn, row["id"])
            if attempt is None or (
                attempt["status"] == "intent" and not attempt["send_granted_at"]
            ):
                await self._void(conn, row, attempt, actor="session", reason="session_closed")
        return event

    async def abandon(
        self,
        proposal_id: str,
        *,
        lease: Lease,
        turn_id: str,
        actor: str,
        reason: str,
    ) -> ActionProposal:
        async with self.store.transaction() as conn:
            await self.sessions.require_lease(conn, lease, main_only=True)
            await self.sessions.require_turn(conn, lease, turn_id)
            row = await self._proposal(conn, proposal_id)
            self._check_scope(row, lease, turn_id)
            attempt = await self._attempt(conn, proposal_id)
            if attempt is not None and attempt["status"] != "voided":
                raise ActionError("already_claimed", "处置已有执行尝试，不能由轮次放弃")
            if row["status"] in OPEN_UNSENT and attempt is None:
                lock = await self._lock(conn, _key(row))
                if lock and lock["proposal_id"] == proposal_id:
                    raise ActionError("target_busy", "处置已领取，不能由轮次放弃")
                await self._void(conn, row, None, actor=actor, reason=reason)
                row["status"] = "voided"
            await self.sessions.require_lease(conn, lease, main_only=True)
            return _proposal_from_row(row)

    async def reserve(
        self,
        proposal_id: str,
        *,
        lease: Lease,
        turn_id: str,
    ) -> tuple[Reservation | None, ActionAttempt | None]:
        # 恢复旧目标锁使用其所属会话的事务，避免同时持有两个会话行锁。
        proposal = await self.store.get_proposal(proposal_id)
        if proposal is None:
            raise ActionError("not_found", "提案不存在")
        if (proposal.session_id, proposal.session_turn_id) != (lease.session_id, turn_id):
            raise ActionError("scope_mismatch", "提案不属于当前主线轮次")
        async with self.store.transaction() as conn:
            await self.sessions.require_lease(conn, lease, main_only=True)
            await self.sessions.require_turn(conn, lease, turn_id)
        key = target_key(
            proposal.target.cluster,
            proposal.target.namespace,
            proposal.target.kind,
            proposal.target.name,
        )
        await self.recover_target(key)
        async with self.store.transaction() as conn:
            await self.sessions.require_lease(conn, lease, main_only=True)
            await self.sessions.require_turn(conn, lease, turn_id)
            row = await self._proposal(conn, proposal_id)
            self._check_scope(row, lease, turn_id)
            attempt = await self._attempt(conn, proposal_id)
            if attempt:
                if attempt["status"] == "intent":
                    raise ActionError("target_busy", "原执行仍在准备")
                return None, _attempt_from_row(attempt)
            if row["status"] != "approved":
                raise ActionError("not_approved", f"提案状态为 {row['status']}，不能执行")
            if await self._legacy_may_have_sent(conn, key):
                raise ActionError("legacy_target_busy", "存在可能已发送的旧动作，需先完成切换核实")
            owner = uuid4().hex
            lock = await _one(
                conn,
                """
                INSERT INTO action_locks (
                    target_key, proposal_id, owner, generation, expires_at,
                    session_owner, session_generation
                ) VALUES (%s, %s, %s, nextval('action_lock_generation'),
                          clock_timestamp() + %s * interval '1 second', %s, %s)
                ON CONFLICT (target_key) DO NOTHING
                RETURNING generation
                """,
                (key, proposal_id, owner, ACTION_LOCK_SECONDS, lease.owner, lease.generation),
            )
            if lock is None:
                raise ActionError("target_busy", "目标已有未完成的处置")
            await self.sessions.require_lease(conn, lease, main_only=True)
            return Reservation(proposal_id, key, owner, lock["generation"], lease, turn_id), None

    async def renew(self, reservation: Reservation) -> bool:
        async with self.store.transaction() as conn:
            row, attempt, lock = await self._locked_reservation(conn, reservation)
            if not self._owns(reservation, lock):
                return False
            sent = (
                attempt is not None
                and attempt["status"] == "executing"
                and attempt["send_granted_at"]
            )
            if not sent:
                if row["status"] != "approved":
                    return False
                try:
                    await self.sessions.require_lease(conn, reservation.lease, main_only=True)
                    await self.sessions.require_turn(conn, reservation.lease, reservation.turn_id)
                except LeaseLost:
                    return False
            updated = await conn.execute(
                """
                UPDATE action_locks SET expires_at = clock_timestamp() + %s * interval '1 second'
                WHERE target_key = %s AND generation = %s AND expires_at > clock_timestamp()
                """,
                (ACTION_LOCK_SECONDS, reservation.key, reservation.generation),
            )
            return updated.rowcount == 1

    async def authorization_current(self, approval_id: str) -> bool:
        async with self.store.transaction() as conn:
            return (
                await _one(
                    conn,
                    """
                SELECT 1 FROM approvals WHERE id = %s AND decision = 'approved'
                  AND revoked_at IS NULL AND expires_at > clock_timestamp()
                """,
                    (approval_id,),
                )
                is not None
            )

    async def save_intent(self, reservation: Reservation, attempt: ActionAttempt) -> None:
        async with self.store.transaction() as conn:
            row, existing, lock = await self._locked_reservation(conn, reservation)
            await self._require_unsent(conn, reservation, row, lock)
            if existing is not None:
                raise ActionError("already_executed", "提案已有执行记录")
            await self.store._insert_attempt(conn, attempt)
            await self._emit_result(conn, row, attempt)
            await self.sessions.require_lease(conn, reservation.lease, main_only=True)

    async def grant_send(
        self,
        reservation: Reservation,
        attempt: ActionAttempt,
        approval: Approval,
    ) -> ActionAttempt:
        expired = False
        async with self.store.transaction() as conn:
            row, saved, lock = await self._locked_reservation(conn, reservation)
            await self._require_unsent(conn, reservation, row, lock)
            if saved is None or saved["id"] != attempt.id or saved["status"] != "intent":
                raise ActionError("attempt_lost", "发送意图已改变")
            current = await _one(
                conn, "SELECT * FROM approvals WHERE id = %s FOR UPDATE", (approval.id,)
            )
            if (
                current is None
                or current["proposal_id"] != reservation.proposal_id
                or current["revoked_at"] is not None
                or current["decision"] != "approved"
                or current["proposal_hash"] != proposal_hash(_proposal_from_row(row))
                or (row["published_seq"] is None and row["risk_disposition"] != "auto")
            ):
                raise ActionError("no_approval", "找不到匹配当前卡片的有效批准")
            if row["risk_disposition"] == "approval":
                decision_event = await _one(
                    conn,
                    """
                    SELECT 1 FROM session_events
                    WHERE session_id = %s AND turn_id = %s AND type = 'card_decision'
                      AND payload->>'card_id' = %s AND payload->>'decision_id' = %s
                      AND payload->>'status' = 'approved'
                    """,
                    (row["session_id"], row["session_turn_id"], row["id"], approval.id),
                )
                if decision_event is None:
                    raise ActionError("no_approval", "找不到这张卡片的审批事件")
            elif not (
                row["risk_disposition"] == "auto"
                and row["target_kind"] == "Pod"
                and row["kind"] == "restart_pod"
                and row["namespace"] in AUTO_NAMESPACES
                and current["actor"] == f"policy:{row['policy_version']}"
            ):
                raise ActionError("no_approval", "动作不满足自动授权例外")
            if row["investigation_id"] is not None:
                linked = await _one(
                    conn,
                    """
                    SELECT 1 FROM tool_requests
                    WHERE investigation_id = %s AND proposal_id = %s AND state = 'action_pending'
                    """,
                    (row["investigation_id"], row["id"]),
                )
                if linked is None:
                    raise ActionError("action_unbound", "轮次工具账本尚未关联待执行动作")
            lease = reservation.lease
            updated = await _one(
                conn,
                """
                UPDATE action_attempts a
                SET status = 'executing', send_granted_at = clock_timestamp(),
                    request_timeout_seconds = %s, result = '{"stage":"send_granted"}'::jsonb
                FROM sessions s, action_locks l, approvals d
                WHERE a.id = %s AND a.status = 'intent' AND s.id = %s AND s.closed_at IS NULL
                  AND s.lease_owner = %s AND s.lease_generation = %s
                  AND s.lease_expires_at > clock_timestamp()
                  AND l.target_key = %s AND l.owner = %s AND l.generation = %s
                  AND l.expires_at > clock_timestamp() AND d.id = %s
                  AND d.expires_at > clock_timestamp()
                RETURNING a.*
                """,
                (
                    WRITE_TIMEOUT_SECONDS,
                    attempt.id,
                    lease.session_id,
                    lease.owner,
                    lease.generation,
                    reservation.key,
                    reservation.owner,
                    reservation.generation,
                    approval.id,
                ),
            )
            if updated is None:
                await self._require_unsent(conn, reservation, row, lock)
                if current["expires_at"] is None or current["expires_at"] <= await _now(conn):
                    await self._end_unsent(conn, row, saved, "expired", code="expired")
                    expired = True
                else:
                    raise LeaseLost(lease.session_id)
            else:
                await self._status(conn, row["id"], "executing")
                receipt = _attempt_from_row(updated)
                await self._emit_result(conn, row, receipt, status="executing")
                await self.audit(
                    conn,
                    row["id"],
                    approval.actor,
                    "action.send_granted",
                    {"attempt_id": attempt.id},
                )
        if expired:
            raise ActionError("expired", "审批已过期，未发送写请求")
        return receipt

    async def fail_unsent(
        self, reservation: Reservation, *, status: str, code: str, reason: str = ""
    ) -> None:
        async with self.store.transaction() as conn:
            row, attempt, lock = await self._locked_reservation(conn, reservation)
            await self._require_unsent(conn, reservation, row, lock)
            if attempt and (attempt["status"] != "intent" or attempt["send_granted_at"]):
                raise ActionError("already_executed", "动作可能已发送")
            await self._end_unsent(conn, row, attempt, status, code=code, reason=reason)

    async def finish(
        self,
        reservation: Reservation,
        attempt: ActionAttempt,
        *,
        status: AttemptStatus,
        proposal_status: ProposalStatus,
        result: dict | None,
        verification: dict | None,
        followup: ActionFollowup | None,
    ) -> ActionAttempt:
        async with self.store.transaction() as conn:
            row, saved, lock = await self._locked_reservation(conn, reservation)
            if saved is None or saved["id"] != attempt.id:
                raise ActionError("attempt_lost", "执行尝试已改变")
            if (
                not self._owns(reservation, lock)
                or not saved["send_granted_at"]
                or saved["status"] != "executing"
                or row["status"] != "executing"
            ):
                return _attempt_from_row(saved)
            updated = await self.finish_locked(
                conn,
                row,
                saved,
                status.value,
                proposal_status.value,
                result=result,
                verification=verification,
                followup=followup,
            )
            return updated

    async def recover_target(self, key: str) -> None:
        async with self.store.transaction() as conn:
            reference = await _one(
                conn,
                """
                SELECT l.proposal_id, p.session_id FROM action_locks l
                LEFT JOIN action_proposals p ON p.id = l.proposal_id WHERE l.target_key = %s
                """,
                (key,),
            )
            if reference is None or reference["session_id"] is None:
                # 遗留锁只能由切换流程处置。
                return
            await self.sessions.lock_session(conn, reference["session_id"])
            row = await self._proposal(conn, reference["proposal_id"])
            attempt = await self._attempt(conn, row["id"])
            lock = await self._lock(conn, key)
            if lock is None or lock["proposal_id"] != row["id"] or lock["expires_at"] is None:
                return
            if attempt and attempt["status"] in {"verified", "failed", "voided"}:
                await self._unlock(conn, row)
                return
            if attempt and attempt["status"] == "unknown":
                return
            live = lock["expires_at"] > await _now(conn)
            if attempt and attempt["status"] == "executing":
                if not live:
                    await self.mark_unknown(conn, row, attempt)
                return
            session_matches = await _one(
                conn,
                """
                SELECT 1 FROM sessions WHERE id = %s AND closed_at IS NULL
                  AND lease_owner = %s AND lease_generation = %s
                  AND lease_expires_at > clock_timestamp()
                """,
                (row["session_id"], lock["session_owner"], lock["session_generation"]),
            )
            if not live or session_matches is None:
                await self._void(conn, row, attempt, actor="recovery", reason="lease_lost")

    async def recover_expired_targets(self, *, limit: int = 50) -> None:
        """包括已关闭会话，按已有发送权记录收敛，绝不重发请求。"""
        async with self.store.transaction() as conn:
            rows = await conn.execute(
                """
                SELECT l.target_key FROM action_locks l
                JOIN action_proposals p ON p.id = l.proposal_id
                LEFT JOIN action_attempts a ON a.proposal_id = p.id
                WHERE p.session_id IS NOT NULL AND l.expires_at <= clock_timestamp()
                  AND a.status IS DISTINCT FROM 'unknown'
                ORDER BY l.expires_at, l.target_key LIMIT %s
                """,
                (limit,),
            )
            keys = [row["target_key"] for row in await rows.fetchall()]
        for key in keys:
            await self.recover_target(key)

    async def target_is_locked(self, key: str) -> bool:
        await self.recover_target(key)
        async with self.store.transaction() as conn:
            return await _one(
                conn,
                "SELECT 1 FROM action_locks WHERE target_key = %s",
                (key,),
            ) is not None or await self._legacy_may_have_sent(conn, key)

    @staticmethod
    async def _legacy_may_have_sent(conn: AsyncConnection, key: str) -> bool:
        return (
            await _one(
                conn,
                """
            SELECT 1 FROM action_proposals p
            LEFT JOIN action_attempts a ON a.proposal_id = p.id
            WHERE p.session_id IS NULL
              AND concat_ws('/', p.cluster, p.namespace, p.target_kind, p.target_name) = %s
              AND (p.status IN ('executing', 'unknown') OR a.status IN ('executing', 'unknown'))
            LIMIT 1
            """,
                (key,),
            )
            is not None
        )

    async def finish_locked(
        self,
        conn: AsyncConnection,
        row: dict,
        attempt: dict,
        status: str,
        proposal_status: str,
        *,
        result: dict | None,
        verification: dict | None,
        followup: ActionFollowup | None,
    ) -> ActionAttempt:
        updated = await _one(
            conn,
            """
            UPDATE action_attempts SET status = %s, result = %s, verification = %s,
                finished_at = clock_timestamp()
            WHERE id = %s AND status = %s RETURNING *
            """,
            (
                status,
                Jsonb(result),
                Jsonb(verification),
                attempt["id"],
                attempt["status"],
            ),
        )
        if updated is None:
            raise ActionError("attempt_lost", "执行尝试已改变")
        await self._status(conn, row["id"], proposal_status)
        if followup is not None:
            await self.store._insert_followup(conn, followup)
        if status != "unknown":
            await self._unlock(conn, row)
        receipt = _attempt_from_row(updated)
        await self._emit_result(conn, row, receipt, status=proposal_status)
        await self.audit(
            conn,
            row["id"],
            "action-executor",
            f"action.{proposal_status}",
            {"attempt_id": attempt["id"], "attempt_status": status},
        )
        return receipt

    async def mark_unknown(self, conn: AsyncConnection, row: dict, attempt: dict) -> ActionAttempt:
        reason = "发送权已取得，原执行结果待人工核实"
        existing_followup = await _one(
            conn,
            "SELECT 1 FROM action_followups WHERE attempt_id = %s",
            (attempt["id"],),
        )
        followup = (
            None
            if existing_followup
            else ActionFollowup(
                id=f"fol-{uuid4().hex}",
                proposal_id=row["id"],
                attempt_id=attempt["id"],
                cluster=row["cluster"],
                namespace=row["namespace"],
                target_kind=row["target_kind"],
                target_name=row["target_name"],
                outcome=AttemptOutcome.UNKNOWN,
                reason=reason,
                created_at=await _now(conn),
            )
        )
        return await self.finish_locked(
            conn,
            row,
            attempt,
            "unknown",
            "unknown",
            result={**(attempt["result"] or {}), "stage": "interrupted"},
            verification={"outcome": "unknown", "outcome_reason": reason},
            followup=followup,
        )

    async def _end_unsent(
        self,
        conn: AsyncConnection,
        row: dict,
        attempt: dict | None,
        status: str,
        *,
        code: str,
        actor: str = "action-executor",
        reason: str = "",
    ) -> None:
        await self._status(conn, row["id"], status)
        await conn.execute(
            """
            UPDATE approvals SET revoked_at = clock_timestamp()
            WHERE proposal_id = %s AND decision = 'approved' AND revoked_at IS NULL
            """,
            (row["id"],),
        )
        receipt = None
        if attempt is not None:
            updated = await _one(
                conn,
                """
                UPDATE action_attempts SET status = 'voided', finished_at = clock_timestamp(),
                    result = %s WHERE id = %s AND status = 'intent' AND send_granted_at IS NULL
                RETURNING *
                """,
                (Jsonb({"code": code}), attempt["id"]),
            )
            if updated is None:
                raise ActionError("attempt_lost", "执行尝试不再处于未发送状态")
            receipt = _attempt_from_row(updated)
        await self._unlock(conn, row)
        await self._emit_result(conn, row, receipt, status=status, reason=reason or code)
        await self.audit(conn, row["id"], actor, f"action.{status}", {"code": code})

    async def _void(
        self,
        conn: AsyncConnection,
        row: dict,
        attempt: dict | None,
        *,
        actor: str,
        reason: str,
    ) -> None:
        await self._end_unsent(conn, row, attempt, "voided", code=reason, actor=actor)

    async def _locked_reservation(
        self,
        conn: AsyncConnection,
        reservation: Reservation,
    ) -> tuple[dict, dict | None, dict | None]:
        await self.sessions.lock_session(conn, reservation.lease.session_id)
        row = await self._proposal(conn, reservation.proposal_id)
        self._check_scope(row, reservation.lease, reservation.turn_id)
        attempt = await self._attempt(conn, row["id"])
        lock = await self._lock(conn, reservation.key)
        return row, attempt, lock

    async def _require_unsent(
        self,
        conn: AsyncConnection,
        reservation: Reservation,
        row: dict,
        lock: dict | None,
    ) -> None:
        if row["status"] != "approved" or not self._owns(reservation, lock):
            raise ActionError("reservation_lost", "未发送的处置已作废或目标锁已变化")
        if lock["expires_at"] <= await _now(conn):
            raise ActionError("reservation_lost", "动作锁已过期")
        await self.sessions.require_lease(conn, reservation.lease, main_only=True)
        await self.sessions.require_turn(conn, reservation.lease, reservation.turn_id)

    @staticmethod
    def _check_scope(row: dict, lease: Lease, turn_id: str) -> None:
        if (row["session_id"], row["session_turn_id"]) != (lease.session_id, turn_id):
            raise ActionError("scope_mismatch", "提案不属于当前主线轮次")

    @staticmethod
    def _owns(reservation: Reservation, lock: dict | None) -> bool:
        return bool(
            lock
            and (
                lock["proposal_id"],
                lock["owner"],
                lock["generation"],
            )
            == (reservation.proposal_id, reservation.owner, reservation.generation)
        )

    @staticmethod
    async def _proposal(
        conn: AsyncConnection,
        proposal_id: str,
        *,
        session_id: str | None = None,
    ) -> dict:
        row = await _one(
            conn,
            """
            SELECT * FROM action_proposals WHERE id = %s AND (%s::text IS NULL OR session_id = %s)
            FOR UPDATE
            """,
            (proposal_id, session_id, session_id),
        )
        if row is None:
            raise ActionError("not_found", "提案不存在")
        return row

    @staticmethod
    async def _attempt(conn: AsyncConnection, proposal_id: str) -> dict | None:
        return await _one(
            conn, "SELECT * FROM action_attempts WHERE proposal_id = %s FOR UPDATE", (proposal_id,)
        )

    @staticmethod
    async def _lock(conn: AsyncConnection, key: str) -> dict | None:
        return await _one(
            conn, "SELECT * FROM action_locks WHERE target_key = %s FOR UPDATE", (key,)
        )

    @staticmethod
    async def _unlock(conn: AsyncConnection, row: dict) -> None:
        await conn.execute(
            "DELETE FROM action_locks WHERE target_key = %s AND proposal_id = %s",
            (_key(row), row["id"]),
        )

    @staticmethod
    async def _status(conn: AsyncConnection, proposal_id: str, status: str) -> None:
        await conn.execute(
            "UPDATE action_proposals SET status = %s WHERE id = %s", (status, proposal_id)
        )

    async def _emit_result(
        self,
        conn: AsyncConnection,
        row: dict,
        attempt: ActionAttempt | None,
        *,
        status: str | None = None,
        reason: str = "",
    ) -> None:
        if row["session_id"] is not None:
            payload = project_action_result(attempt, proposal_id=row["id"], status=status)
            payload["proposal_status"] = status or row["status"]
            if reason:
                payload["reason"] = project_text(reason)
            await self.sessions.append_event(
                conn,
                row["session_id"],
                "action_result",
                payload,
                turn_id=row["session_turn_id"],
                allow_closed=True,
            )

    @staticmethod
    async def audit(
        conn: AsyncConnection,
        proposal_id: str,
        actor: str,
        event: str,
        payload: dict,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO audit_events (id, correlation_id, actor, event, occurred_at, payload)
            VALUES (%s, %s, %s, %s, clock_timestamp(), %s)
            """,
            (f"audit-{uuid4().hex}", proposal_id, actor, event, Jsonb(payload)),
        )
