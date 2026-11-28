"""持久处置事务。按会话、提案、任务、尝试、资源键顺序加锁。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from aiops.actions.kubernetes_write import WRITE_TIMEOUT_SECONDS
from aiops.actions.policy import target_key
from aiops.actions.remediation_alerts import fault_is_not_clear, lock_workload_alerts
from aiops.actions.remediation_faults import (
    has_inspection_signal,
    inspection_can_clear,
    lock_fault,
    previous_uid_blocks,
)
from aiops.actions.rollout_undo import ActionError
from aiops.actions.safety import ACTION_LOCK_SECONDS, ActionSafety, _now, _one, proposal_hash
from aiops.contracts import (
    ActionAttempt,
    ActionFollowup,
    ActionProposal,
    ApprovalDecision,
    AttemptOutcome,
    AttemptStatus,
    ProposalStatus,
    RiskDecision,
    RiskDisposition,
)
from aiops.db import Store, _attempt_from_row, _proposal_from_row
from aiops.inspection.evidence import latest_target_observations, review_samples, values
from aiops.inspection.store import InspectionStore
from aiops.sessions.models import Lease, LeaseLost
from aiops.sessions.projection import project_payload, project_text


def resource_keys(proposal: ActionProposal) -> list[str]:
    target = proposal.target
    keys = [target_key(target.cluster, target.namespace, target.kind, target.name)]
    if proposal.kind.value == "update_config" and proposal.parameters.get("source") == "config_map":
        keys.append(
            target_key(
                target.cluster, target.namespace, "ConfigMap", proposal.parameters["config_map"]
            )
        )
    return sorted(keys)


@dataclass(frozen=True)
class JobClaim:
    proposal: ActionProposal
    job: dict[str, Any]
    attempt: ActionAttempt | None


class RemediationStore:
    def __init__(
        self, store: Store, safety: ActionSafety, *, inspections: InspectionStore | None = None
    ) -> None:
        self.inspections = inspections or InspectionStore(store)
        self.store = store
        self.safety = safety
        self.sessions = safety.sessions

    async def create(
        self,
        conn: AsyncConnection,
        proposal: ActionProposal,
        risk: RiskDecision,
        evidence: dict,
        *,
        fault_id: str | None = None,
        parent_id: str | None = None,
        inspection_review_id: str | None = None,
    ) -> None:
        await self.store._insert_proposal(conn, proposal, risk)
        if risk.disposition is RiskDisposition.DENY:
            return
        automatic = risk.disposition is RiskDisposition.AUTO
        if automatic:
            await self.safety._insert_decision(
                conn,
                proposal,
                ApprovalDecision.APPROVED,
                f"policy:{risk.policy_version}",
                "自动授权：" + "、".join(risk.reasons),
            )
        await conn.execute(
            """
            INSERT INTO remediation_jobs
                (proposal_id, fault_id, parent_proposal_id, automated,
                 resource_keys, state, evidence, inspection_review_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                proposal.id,
                fault_id,
                parent_id,
                automatic,
                resource_keys(proposal),
                "ready" if automatic else "awaiting_approval",
                Jsonb(evidence),
                inspection_review_id,
            ),
        )

    async def save_manual(
        self,
        proposal: ActionProposal,
        risk: RiskDecision,
        evidence: dict,
        *,
        lease: Lease,
        turn_id: str,
    ) -> None:
        async with self.store.transaction() as conn:
            await self.sessions.lock_session(conn, lease.session_id)
            await self.sessions.require_lease(conn, lease, main_only=True)
            await self.sessions.require_turn(conn, lease, turn_id)
            if (proposal.session_id, proposal.session_turn_id) != (lease.session_id, turn_id):
                raise LeaseLost(turn_id)
            await self.create(conn, proposal, risk, evidence)

    async def publish_rule(self, conn: AsyncConnection, proposal: ActionProposal) -> None:
        row = await self.safety._proposal(conn, proposal.id)
        if row["risk_disposition"] == "deny":
            await self.safety._emit_result(conn, row, None, reason="、".join(row["risk_reasons"]))
            return
        card = await self.safety._card(conn, row)
        event = await self.sessions.append_event(
            conn,
            proposal.session_id,
            "approval_required",
            card,
            turn_id=proposal.session_turn_id,
            allow_closed=proposal.observation_conditions.get("compensates") is not None,
        )
        await conn.execute(
            "UPDATE action_proposals SET published_seq = %s WHERE id = %s",
            (event.seq, proposal.id),
        )

    async def prepare_failed_card(self, proposal_id: str) -> None:
        proposal = await self.store.get_proposal(proposal_id)
        if proposal is None:
            return
        async with self.store.transaction() as conn:
            row, job, attempt, _ = await self._records(conn, proposal_id, proposal.session_id)
            if job is not None and job["state"] == "ready" and job["sent_at"] is None:
                now = await _now(conn)
                if job["expires_at"] is None or job["expires_at"] <= now:
                    await self.safety._end_unsent(
                        conn,
                        row,
                        attempt,
                        "failed",
                        code="target_busy",
                        reason="目标已有另一项未完成的处置，本次未写入",
                    )
                    await self._inspection_receipt(conn, row, job, "failed", code="target_busy")

    async def due(self, limit: int = 20) -> list[str]:
        async with self.store.transaction() as conn:
            rows = await conn.execute(
                """
                SELECT proposal_id FROM remediation_jobs
                WHERE state IN ('ready', 'sending', 'applying', 'observing')
                  AND run_after <= clock_timestamp()
                  AND (expires_at IS NULL OR expires_at <= clock_timestamp())
                ORDER BY run_after, proposal_id LIMIT %s
                """,
                (limit,),
            )
            return [row["proposal_id"] for row in await rows.fetchall()]

    async def _records(
        self, conn: AsyncConnection, proposal_id: str, session_id: str
    ) -> tuple[dict, dict | None, dict | None, list[dict]]:
        await self.sessions.lock_session(conn, session_id)
        row = await self.safety._proposal(conn, proposal_id, session_id=session_id)
        job = await _one(
            conn, "SELECT * FROM remediation_jobs WHERE proposal_id = %s FOR UPDATE", (proposal_id,)
        )
        attempt = await self.safety._attempt(conn, proposal_id)
        keys = job["resource_keys"] if job is not None else []
        records = await conn.execute(
            """
            SELECT * FROM action_locks WHERE target_key = ANY(%s) ORDER BY target_key FOR UPDATE
            """,
            (keys,),
        )
        return row, job, attempt, await records.fetchall()

    async def claim(self, proposal_id: str) -> JobClaim | None:
        proposal = await self.store.get_proposal(proposal_id)
        if proposal is None or proposal.session_id is None:
            return None
        async with self.store.transaction() as conn:
            row, job, attempt, locks = await self._records(conn, proposal_id, proposal.session_id)
            now = await _now(conn)
            if (
                job is None
                or job["state"] in {"finished", "awaiting_approval"}
                or job["run_after"] > now
                or (job["expires_at"] and job["expires_at"] > now)
            ):
                return None
            if job["state"] == "sending":
                await self.safety.mark_unknown(conn, row, attempt)
                await self._done(conn, proposal_id)
                await self._inspection_receipt(conn, row, job, "unknown")
                return None
            if job["sent_at"] is None and row["status"] != "approved":
                await self._done(conn, proposal_id)
                await self._inspection_receipt(conn, row, job, row["status"])
                return None
            if (
                job["inspection_review_id"]
                and job["sent_at"] is None
                and not job["parent_proposal_id"]
            ):
                try:
                    await self._active_inspection(conn, _proposal_from_row(row), job)
                except ActionError as exc:
                    await self.safety._end_unsent(conn, row, attempt, "voided", code=exc.code)
                    await self._inspection_receipt(conn, row, job, "voided", code=exc.code)
                    return None
            if any(lock["proposal_id"] != proposal_id for lock in locks):
                if job["sent_at"] is not None:
                    raise ActionError("lock_corrupt", "写入后锁组不完整")
                await self.safety._end_unsent(conn, row, attempt, "failed", code="target_busy")
                await self._inspection_receipt(conn, row, job, "failed", code="target_busy")
                return None
            keys = job["resource_keys"]
            if (job["sent_at"] is not None or job["parent_proposal_id"] is not None) and len(
                locks
            ) != len(keys):
                raise ActionError("lock_corrupt", "无法接管不完整的锁组")
            for key in keys:
                if await self.safety._legacy_may_have_sent(conn, key):
                    if job["sent_at"] is not None:
                        raise ActionError("lock_corrupt", "目标有未结案的旧动作")
                    await self.safety._end_unsent(
                        conn, row, attempt, "failed", code="legacy_target_busy"
                    )
                    await self._inspection_receipt(
                        conn, row, job, "failed", code="legacy_target_busy"
                    )
                    return None
            owner = uuid4().hex
            generation = (await _one(conn, "SELECT nextval('action_lock_generation') AS value"))[
                "value"
            ]
            expires = now + timedelta(seconds=ACTION_LOCK_SECONDS)
            for key in keys:
                held = await _one(
                    conn,
                    """
                    INSERT INTO action_locks
                        (target_key, proposal_id, owner, generation, expires_at,
                            session_owner, session_generation)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (target_key) DO UPDATE SET owner = EXCLUDED.owner,
                        generation = EXCLUDED.generation, expires_at = EXCLUDED.expires_at,
                        session_owner = EXCLUDED.session_owner,
                        session_generation = EXCLUDED.session_generation
                    WHERE action_locks.proposal_id = EXCLUDED.proposal_id
                    RETURNING target_key
                    """,
                    (key, proposal_id, owner, generation, expires, owner, generation),
                )
                if held is None:
                    # 回滚整组，保证争用时不会持有半组锁。
                    raise ActionError("target_busy", "目标已有未完成的处置")
            updated = await _one(
                conn,
                """
                UPDATE remediation_jobs SET owner = %s, generation = %s, expires_at = %s
                WHERE proposal_id = %s RETURNING *
                """,
                (owner, generation, expires, proposal_id),
            )
            return JobClaim(
                _proposal_from_row(row), updated, _attempt_from_row(attempt) if attempt else None
            )

    async def _require_claim(
        self, conn: AsyncConnection, claim: JobClaim
    ) -> tuple[dict, dict, dict | None]:
        proposal = claim.proposal
        row, job, attempt, locks = await self._records(conn, proposal.id, proposal.session_id)
        now = await _now(conn)
        if (
            job is None
            or job["owner"] != claim.job["owner"]
            or job["generation"] != claim.job["generation"]
            or job["expires_at"] <= now
            or [lock["target_key"] for lock in locks] != job["resource_keys"]
            or any(
                lock["proposal_id"] != proposal.id
                or lock["owner"] != job["owner"]
                or lock["generation"] != job["generation"]
                or lock["expires_at"] <= now
                for lock in locks
            )
        ):
            raise LeaseLost(proposal.id)
        return row, job, attempt

    async def renew(self, claim: JobClaim) -> None:
        async with self.store.transaction() as conn:
            await self._require_claim(conn, claim)
            deadline = await _now(conn) + timedelta(seconds=ACTION_LOCK_SECONDS)
            await conn.execute(
                "UPDATE remediation_jobs SET expires_at = %s WHERE proposal_id = %s",
                (deadline, claim.proposal.id),
            )
            await conn.execute(
                "UPDATE action_locks SET expires_at = %s WHERE proposal_id = %s",
                (deadline, claim.proposal.id),
            )

    async def _active_inspection(self, conn, proposal, job):
        review = await _one(
            conn,
            """
            SELECT r.* FROM remediation_inspection_reviews r
            JOIN investigations i ON i.id = r.investigation_id
            JOIN incidents n ON n.id = i.incident_id
            WHERE r.id = %s AND r.fault_id = %s AND r.turn_id = %s
              AND i.session_turn_id = r.turn_id AND i.status = 'completed'
              AND n.trigger = 'scheduled'
            """,
            (job["inspection_review_id"], job["fault_id"], proposal.session_turn_id),
        )
        if review is None or review["investigation_id"] != proposal.investigation_id:
            raise ActionError("inspection_review_missing", "缺少本次巡检核对回执")
        try:
            run, now = await self.inspections.require_active_for_send(
                conn, run_id=review["run_id"], session_id=proposal.session_id
            )
        except LeaseLost:
            raise ActionError("inspection_expired", "巡检轮次已结束或超过期限") from None
        if run.sop_hash != review["sop_hash"] or run.cluster != proposal.target.cluster:
            raise ActionError("inspection_scope_changed", "巡检回执归属不一致")
        return review, run, now

    async def _verify_inspection(self, conn, proposal, job) -> datetime:
        review, run, _ = await self._active_inspection(conn, proposal, job)
        trusted = job["evidence"]["trusted"]
        required = set(trusted["inspection_values"])
        target = proposal.target
        observations = await latest_target_observations(
            conn, run.id, {"namespace": target.namespace, "kind": target.kind, "name": target.name}
        )
        now = await self.inspections.now(conn)
        if now >= min(run.deadline, run.lease_expires_at):
            raise ActionError("inspection_expired", "巡检轮次已结束或超过期限")
        samples = review_samples(
            run,
            observations,
            uid=target.uid,
            trigger=review["trigger_check"],
            required=required,
            max_age=review["max_evidence_age_seconds"],
            now=now,
        )
        if (
            not samples
            or trusted["source"] != "inspection"
            or trusted["rule_id"] != review["rule_id"]
            or trusted["policy_version"] != review["policy_version"]
            or set(review["observation_ids"]) != {item.id for item in samples}
            or set(proposal.evidence_ids) != set(review["observation_ids"])
            or trusted["inspection_values"] != {item.check_id: values(item) for item in samples}
            or datetime.fromisoformat(trusted["observed_at"])
            != min(item.sampled_at for item in samples)
        ):
            raise ActionError("inspection_evidence_changed", "巡检证据已更新、缺失或过期")
        return now

    async def _inspection_receipt(self, conn, row: dict, job: dict, status: str, **details) -> None:
        if job["inspection_review_id"] is None:
            return
        bound = await _one(
            conn,
            """
            SELECT r.id, r.result
            FROM remediation_inspection_reviews v JOIN inspection_runs r ON r.id = v.run_id
            JOIN remediation_jobs j ON j.inspection_review_id = v.id AND j.fault_id = v.fault_id
            WHERE j.proposal_id = %s AND r.session_id = %s
            """,
            (row["id"], row["session_id"]),
        )
        if bound is None:
            raise ActionError("inspection_receipt_unbound", "处置回执缺少持久巡检归属")
        receipt = {"status": status, "fault_id": job["fault_id"], **details}
        if job["parent_proposal_id"] is not None:
            receipt["parent_id"] = job["parent_proposal_id"]
        actions = bound["result"].get("actions", {}) | {row["id"]: receipt}
        await self.inspections.record_receipt_result(
            conn, run_id=bound["id"], session_id=row["session_id"], result={"actions": actions}
        )

    async def grant_send(
        self,
        claim: JobClaim,
        attempt: ActionAttempt,
        *,
        authorize: Callable[[ActionProposal, dict, datetime], None],
    ) -> ActionAttempt:
        async with self.store.transaction() as conn:
            target = claim.proposal.target
            await lock_workload_alerts(conn, target.cluster, target.namespace, target.name)
            row, job, existing = await self._require_claim(conn, claim)
            if row["status"] != "approved" or job["state"] != "ready" or existing:
                raise ActionError("not_approved", "已执行或无有效批准")
            proposal = _proposal_from_row(row)
            session = await self.sessions.lock_session(conn, proposal.session_id)
            if (
                session.closed_at is not None
                and job["parent_proposal_id"] is None
                and job["inspection_review_id"] is None
            ):
                raise ActionError("session_closed", "会话已关闭")
            current = await _one(
                conn,
                """
                SELECT * FROM approvals
                WHERE proposal_id = %s AND revoked_at IS NULL
                ORDER BY decided_at DESC, id DESC LIMIT 1 FOR UPDATE
                """,
                (proposal.id,),
            )
            inspection = (
                job["inspection_review_id"] is not None and job["parent_proposal_id"] is None
            )
            if inspection:
                await self._active_inspection(conn, proposal, job)
            now = await _now(conn)
            if (
                current is None
                or current["decision"] != "approved"
                or current["expires_at"] is None
                or current["expires_at"] <= now
                or current["proposal_hash"] != proposal_hash(proposal)
                or proposal_hash(claim.proposal) != proposal_hash(proposal)
            ):
                raise ActionError("no_approval", "找不到与当前提案匹配的有效批准")
            if job["automated"]:
                if current["actor"] != f"policy:{row['policy_version']}":
                    raise ActionError("no_approval", "缺少规则授权")
                fault = await lock_fault(conn, target, job["fault_id"])
                if fault is None or fault["cleared_at"] is not None:
                    raise ActionError("fault_closed", "本次故障已恢复")
                if job["parent_proposal_id"] is None and await previous_uid_blocks(conn, target):
                    raise ActionError("previous_uid_open", "同名旧目标的故障或写入仍待核实")
                if job["parent_proposal_id"] is None and not inspection:
                    active = await _one(
                        conn,
                        """
                        SELECT 1 FROM remediation_alerts WHERE fault_id = %s
                            AND session_id = %s AND resolved_at IS NULL LIMIT 1
                        """,
                        (job["fault_id"], proposal.session_id),
                    )
                    if active is None:
                        raise ActionError("alert_resolved", "当前告警已恢复")
                sent = await _one(
                    conn,
                    """
                    SELECT proposal_id FROM remediation_jobs
                    WHERE fault_id = %s AND automated AND sent_at IS NOT NULL
                      AND (parent_proposal_id IS NOT NULL) = %s
                    """,
                    (job["fault_id"], job["parent_proposal_id"] is not None),
                )
                if sent is not None:
                    raise ActionError("fault_quota", "本次故障的发送名额已使用")
            else:
                published = await _one(
                    conn,
                    """
                    SELECT payload->>'fingerprint' AS fingerprint FROM session_events
                    WHERE session_id = %s AND seq = %s AND type = 'approval_required'
                      AND payload->>'card_id' = %s
                    """,
                    (proposal.session_id, row["published_seq"], proposal.id),
                )
                event = await _one(
                    conn,
                    """
                    SELECT 1 FROM session_events
                    WHERE session_id = %s AND turn_id IS NOT DISTINCT FROM %s
                      AND type = 'card_decision' AND payload->>'card_id' = %s
                      AND payload->>'decision_id' = %s AND payload->>'status' = 'approved'
                    """,
                    (proposal.session_id, proposal.session_turn_id, proposal.id, current["id"]),
                )
                if (
                    published is None
                    or published["fingerprint"] != current["proposal_hash"]
                    or event is None
                ):
                    raise ActionError("no_approval", "缺少绑定卡片的批准事件")
            if proposal.origin.value == "model":
                linked = await _one(
                    conn,
                    """
                    SELECT 1 FROM tool_requests WHERE investigation_id = %s
                    AND proposal_id = %s AND state = 'action_pending'
                    """,
                    (proposal.investigation_id, proposal.id),
                )
                if linked is None:
                    raise ActionError("action_unbound", "调查账本尚未关联执行")
            if inspection:
                now = await self._verify_inspection(conn, proposal, job)
            if current["expires_at"] <= await _now(conn):
                raise ActionError("no_approval", "发送时批准已过期")
            authorize(proposal, job, now)
            await self.store._insert_attempt(conn, attempt)
            if inspection:
                now = await self._verify_inspection(conn, proposal, job)
            saved = await _one(
                conn,
                """
                UPDATE action_attempts SET status = 'executing', send_granted_at = %s,
                    request_timeout_seconds = %s, result = '{"stage":"send_granted"}'
                WHERE id = %s RETURNING *
                """,
                (now, WRITE_TIMEOUT_SECONDS, attempt.id),
            )
            await conn.execute(
                "UPDATE remediation_jobs SET state = 'sending', sent_at = %s "
                "WHERE proposal_id = %s",
                (now, proposal.id),
            )
            await self.safety._status(conn, proposal.id, "executing")
            receipt = _attempt_from_row(saved)
            await self.safety._emit_result(conn, row, receipt, status="executing")
            await self.safety.audit(
                conn,
                proposal.id,
                current["actor"],
                "action.send_granted",
                {
                    "attempt_id": attempt.id,
                    "fault_id": job["fault_id"],
                    "parent_id": job["parent_proposal_id"],
                },
            )
            await self._inspection_receipt(conn, row, job, "send_granted")
            return receipt

    async def applied(self, claim: JobClaim, result: dict) -> None:
        async with self.store.transaction() as conn:
            row, job, attempt = await self._require_claim(conn, claim)
            if job["state"] != "sending":
                raise LeaseLost(claim.proposal.id)
            saved = await _one(
                conn,
                "UPDATE action_attempts SET result = %s WHERE id = %s RETURNING *",
                (Jsonb({"stage": "applied", "written": True, **result}), attempt["id"]),
            )
            applied_at = (
                await self.inspections.now(conn)
                if job["inspection_review_id"]
                else await _now(conn)
            )
            await conn.execute(
                """
                UPDATE remediation_jobs SET state = 'applying', applied_at = %s,
                    owner = NULL, generation = NULL, expires_at = NULL,
                    run_after = clock_timestamp()
                WHERE proposal_id = %s
                """,
                (applied_at, claim.proposal.id),
            )
            await self.safety._emit_result(conn, row, _attempt_from_row(saved), status="executing")
            await self._inspection_receipt(conn, row, job, "applied")

    async def reschedule(
        self,
        claim: JobClaim,
        *,
        delay: float,
        observation: dict | None = None,
        effective_at: datetime | None = None,
    ) -> None:
        async with self.store.transaction() as conn:
            row, job, attempt = await self._require_claim(conn, claim)
            await conn.execute(
                """
                UPDATE remediation_jobs SET
                    state = CASE WHEN %(effective)s::timestamptz IS NOT NULL THEN 'observing'
                        ELSE state END,
                    effective_at = COALESCE(effective_at, %(effective)s),
                    observation = COALESCE(%(observation)s, observation),
                    run_after = clock_timestamp() + %(delay)s * interval '1 second',
                    owner = NULL, generation = NULL, expires_at = NULL
                WHERE proposal_id = %(proposal)s
                """,
                {
                    "effective": effective_at,
                    "observation": Jsonb(observation) if observation is not None else None,
                    "delay": delay,
                    "proposal": claim.proposal.id,
                },
            )
            if observation is not None and observation != job["observation"]:
                await self.sessions.append_event(
                    conn,
                    row["session_id"],
                    "remediation_progress",
                    {"card_id": row["id"], **project_payload(observation)},
                    turn_id=row["session_turn_id"],
                    allow_closed=True,
                )

    async def fail_unsent(self, claim: JobClaim, code: str, reason: str) -> None:
        async with self.store.transaction() as conn:
            row, job, attempt = await self._require_claim(conn, claim)
            if job["sent_at"] is not None:
                raise ActionError("already_executed", "动作可能已发送")
            status = "voided" if code == "inspection_expired" else "failed"
            await self.safety._end_unsent(conn, row, attempt, status, code=code, reason=reason)
            await self._inspection_receipt(conn, row, job, status, code=code)

    async def finish(
        self,
        claim: JobClaim,
        *,
        outcome: AttemptOutcome,
        reason: str,
        result: dict,
        verification: dict,
        keep_locked: bool = False,
        compensation: tuple[ActionProposal, RiskDecision, dict] | None = None,
    ) -> AttemptOutcome:
        async with self.store.transaction() as conn:
            target = claim.proposal.target
            if outcome is AttemptOutcome.IMPROVED and claim.job["fault_id"] is not None:
                await lock_workload_alerts(conn, target.cluster, target.namespace, target.name)
            row, job, attempt = await self._require_claim(conn, claim)
            fault = None
            inspection_fault = False
            if outcome is AttemptOutcome.IMPROVED and job["fault_id"] is not None:
                fault = await lock_fault(conn, target, job["fault_id"])
                if (
                    fault is None
                    or job["effective_at"] is None
                    or await fault_is_not_clear(
                        conn, fault_id=job["fault_id"], started_at=job["effective_at"]
                    )
                ):
                    outcome = AttemptOutcome.NO_IMPROVEMENT
                    reason = "观察期间出现同类告警或原告警仍未恢复，需人工处理"
                else:
                    inspection_fault = await has_inspection_signal(conn, fault["id"])
                    if not inspection_fault and fault["cleared_at"] is None:
                        await conn.execute(
                            "UPDATE remediation_faults SET cleared_at = clock_timestamp() "
                            "WHERE id = %s",
                            (job["fault_id"],),
                        )
            compensated = result.get("compensated") is True
            completed = outcome is AttemptOutcome.IMPROVED or compensated
            status = (
                AttemptStatus.VERIFIED
                if completed
                else (AttemptStatus.UNKNOWN if keep_locked else AttemptStatus.FAILED)
            )
            proposal_status = (
                ProposalStatus.SUCCEEDED
                if completed
                else (ProposalStatus.UNKNOWN if keep_locked else ProposalStatus.FAILED)
            )
            followup = None
            if not completed or compensated:
                followup = ActionFollowup(
                    id=f"fol-{uuid4().hex}",
                    proposal_id=row["id"],
                    attempt_id=attempt["id"],
                    cluster=row["cluster"],
                    namespace=row["namespace"],
                    target_kind=row["target_kind"],
                    target_name=row["target_name"],
                    outcome=outcome,
                    reason=reason,
                    created_at=await _now(conn),
                )
            await self.safety.finish_locked(
                conn,
                row,
                attempt,
                status.value,
                proposal_status.value,
                result=result,
                verification={**verification, "outcome": outcome.value, "outcome_reason": reason},
                followup=followup,
                release_locks=compensation is None,
            )
            await self._done(conn, row["id"])
            if outcome is AttemptOutcome.IMPROVED and inspection_fault:
                await self._clear_after_observation(conn, fault, verification)
            await self._inspection_receipt(
                conn, row, job, proposal_status.value, outcome=outcome.value
            )
            if compensation is not None:
                proposal, risk, evidence = compensation
                if (
                    resource_keys(proposal) != job["resource_keys"]
                    or job["parent_proposal_id"] is not None
                ):
                    raise ActionError("lock_corrupt", "补偿目标与原锁组不一致")
                await self.create(
                    conn,
                    proposal,
                    risk,
                    evidence,
                    fault_id=job["fault_id"],
                    parent_id=row["id"],
                    inspection_review_id=job["inspection_review_id"],
                )
                await conn.execute(
                    """
                    UPDATE action_locks SET proposal_id = %(proposal)s, owner = %(owner)s,
                        generation = %(generation)s, session_owner = %(owner)s,
                        session_generation = %(generation)s,
                        expires_at = %(expires_at)s
                    WHERE proposal_id = %(parent)s
                    """,
                    {
                        "proposal": proposal.id,
                        "owner": uuid4().hex,
                        "generation": (
                            await _one(conn, "SELECT nextval('action_lock_generation') AS value")
                        )["value"],
                        "expires_at": await _now(conn) + timedelta(seconds=ACTION_LOCK_SECONDS),
                        "parent": row["id"],
                    },
                )
                await self.publish_rule(conn, proposal)
                await self._inspection_receipt(
                    conn,
                    {"id": proposal.id, "session_id": proposal.session_id},
                    job,
                    "compensation_ready",
                    parent_id=row["id"],
                )
        return outcome

    async def _clear_after_observation(self, conn, fault: dict, verification: dict) -> None:
        if (
            verification.get("method") != "business_probe_window"
            or not verification.get("started_at")
            or verification.get("failed_slots") != 0
            or verification.get("slots_collected") != verification.get("slots_required")
        ):
            return
        start = datetime.fromisoformat(verification["started_at"])
        end = start + timedelta(
            seconds=verification["slot_seconds"] * verification["slots_required"]
        )
        now = await self.inspections.now(conn)
        if await inspection_can_clear(conn, fault, window_start=start, window_end=end, now=now):
            await conn.execute(
                "UPDATE remediation_faults SET cleared_at = %s WHERE id = %s",
                (now, fault["id"]),
            )

    @staticmethod
    async def _done(conn: AsyncConnection, proposal_id: str) -> None:
        await conn.execute(
            """
            UPDATE remediation_jobs SET state = 'finished', owner = NULL,
                generation = NULL, expires_at = NULL WHERE proposal_id = %s
            """,
            (proposal_id,),
        )

    async def note(self, conn: AsyncConnection, session_id: str, message: str) -> None:
        await self.sessions.append_event(
            conn,
            session_id,
            "message",
            {"role": "assistant", "text": project_text(message)},
        )
