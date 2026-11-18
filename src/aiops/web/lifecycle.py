"""页面会话的审批、租约交接与恢复事务。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from aiops.actions.executor import proposal_hash
from aiops.contracts import Approval, ApprovalDecision
from aiops.db import ConversationConflict, Store, WebRequestResult, _proposal_from_row
from aiops.web.card import card_hash


async def decide_proposal(
    store: Store,
    proposal_id: str,
    *,
    request_id: str,
    decision: ApprovalDecision,
    submitted_hash: str,
    reason: str,
    actor: str,
    ttl: int,
) -> WebRequestResult:
    approval_id = f"apr-{uuid4().hex}"
    async with store._pool.connection() as conn:
        result = await store._claim_web_request(
            conn,
            request_id,
            "decision",
            proposal_id,
            {"decision": decision.value, "card_hash": submitted_hash, "reason": reason},
            approval_id,
        )
        if result.replayed:
            return result
        cur = await conn.execute(
            """
            SELECT c.id, c.investigation_id, t.ready_proposal_id
              FROM action_proposals p
              JOIN conversations c USING (investigation_id)
              JOIN investigation_tasks t USING (investigation_id)
             WHERE p.id = %s
               AND c.id = (SELECT id FROM conversations ORDER BY created_at DESC, id DESC LIMIT 1)
             FOR UPDATE OF t
            """,
            (proposal_id,),
        )
        task = await cur.fetchone()
        if task is None:
            raise ConversationConflict("not_found")
        if task["ready_proposal_id"] != proposal_id:
            raise ConversationConflict("not_waiting")

        cur = await conn.execute(
            "SELECT * FROM action_proposals WHERE id = %s FOR UPDATE", (proposal_id,)
        )
        proposal = _proposal_from_row(await cur.fetchone())
        if submitted_hash != await card_hash(conn, proposal):
            raise ConversationConflict("hash_mismatch")
        cur = await conn.execute(
            "UPDATE action_proposals SET status = %s WHERE id = %s AND status = 'pending_approval'",
            (decision.value, proposal_id),
        )
        if not cur.rowcount:
            raise ConversationConflict("not_pending")
        now = datetime.now(UTC)
        approval = Approval(
            id=approval_id,
            proposal_id=proposal_id,
            proposal_hash=proposal_hash(proposal),
            actor=actor,
            reason=reason,
            decision=decision,
            card_hash=submitted_hash,
            decided_at=now,
            expires_at=now + timedelta(seconds=ttl)
            if decision is ApprovalDecision.APPROVED
            else None,
        )
        await store._insert_approval(conn, approval)
        await conn.execute(
            """
            INSERT INTO audit_events (id, correlation_id, actor, event, occurred_at, payload)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                f"audit-{uuid4().hex}",
                proposal_id,
                actor,
                f"action.{decision.value}",
                now,
                Jsonb(
                    {
                        "approval_id": approval_id,
                        "card_hash": submitted_hash,
                        "proposal_hash": approval.proposal_hash,
                    }
                ),
            ),
        )
        await conn.execute(
            "INSERT INTO web_work (conversation_id, kind, proposal_id) VALUES (%s, 'resume', %s)",
            (task["id"], proposal_id),
        )
        if decision is ApprovalDecision.REJECTED:
            await conn.execute(
                "UPDATE investigation_tasks SET actions_disabled = true "
                "WHERE investigation_id = %s",
                (task["investigation_id"],),
            )
        await store._insert_conversation_event(conn, task["id"], "proposal")
        return result


async def handoff(store: Store, work: dict, proposal_id: str) -> None:
    """图中断存档落盘后，才让此提案出现在可审批状态。"""
    async with store._pool.connection() as conn:
        cur = await conn.execute(
            """
            UPDATE investigation_tasks
               SET ready_proposal_id = proposal_id, lease_owner = NULL, lease_expires_at = NULL
             WHERE investigation_id = %s AND phase = 'waiting_approval' AND proposal_id = %s
               AND lease_owner = %s AND lease_expires_at > clock_timestamp()
            """,
            (work["investigation_id"], proposal_id, work["instance_id"]),
        )
        if not cur.rowcount:
            raise RuntimeError("等待审批交接失败")
        cur = await conn.execute(
            "UPDATE web_work SET state = 'done', finished_at = now() "
            "WHERE id = %s AND state = 'running' AND instance_id = %s",
            (work["id"], work["instance_id"]),
        )
        if not cur.rowcount:
            raise RuntimeError("等待审批工作已失效")
        await store._insert_conversation_event(conn, work["conversation_id"], "proposal")


async def _lock_conversation(conn: AsyncConnection, conversation_id: str) -> dict | None:
    cur = await conn.execute(
        """
        SELECT c.id, c.finished_at, c.investigation_id, t.phase, t.ready_proposal_id,
               t.lease_owner, (t.lease_expires_at > clock_timestamp()) AS live_lease
          FROM investigation_tasks t JOIN conversations c USING (investigation_id)
         WHERE c.id = %s FOR UPDATE OF t
        """,
        (conversation_id,),
    )
    return await cur.fetchone()


async def _latest_action(conn: AsyncConnection, investigation_id: str) -> dict | None:
    cur = await conn.execute(
        """
        SELECT p.id AS proposal_id, p.cluster, p.namespace, p.target_kind, p.target_name,
               p.status AS proposal_status, a.id AS attempt_id, a.status AS attempt_status,
               a.result, f.id AS followup_id, f.status AS followup_status, f.resolution
          FROM action_proposals p
          LEFT JOIN action_attempts a ON a.proposal_id = p.id
          LEFT JOIN action_followups f ON f.attempt_id = a.id
         WHERE p.investigation_id = %s
         ORDER BY p.created_at DESC, p.id DESC LIMIT 1
        """,
        (investigation_id,),
    )
    return await cur.fetchone()


_NOT_EXECUTED = {
    "expired": "授权已过期",
    "drift": "目标已被改动",
    "revision_changed": "历史版本已变化",
    "probe_ambiguous": "业务探针未返回唯一序列",
    "baseline_unavailable": "无法取得业务基线",
    "api_rejected": "写前核对未通过或集群拒绝了写请求",
}
_VERIFICATION = {
    "improved": "首页探针显示业务已改善",
    "no_improvement": "首页探针显示业务没有改善",
    "worsened": "首页探针显示业务恶化",
    "unknown": "业务验证结果未知",
}


def _recorded_outcome(result: dict) -> str:
    """只读收尾时按持久化的执行记录写结论，不让"已写入"和"未执行"读起来一样。"""
    if result.get("send_state") == "not_sent":
        why = _NOT_EXECUTED.get(result.get("not_executed_reason") or "", "执行前核对未通过")
        return f"回滚未执行（{why}），集群未被写入。调查在处置后停止。"
    verdict = _VERIFICATION.get(result.get("verification") or "unknown", "业务验证结果未知")
    return f"回滚已写入集群，{verdict}。调查在处置后停止。"


def _unconfirmed(action: dict | None) -> bool:
    if not action or action["followup_status"] == "resolved" or not action["attempt_id"]:
        return False
    result = action["result"] or {}
    state = result.get("send_state")
    return (
        state in {"sending", "uncertain"}
        or (state == "sent" and result.get("verification_done") is not True)
        or (state is None and action["attempt_status"] in {"intent", "executing"})
    )


async def _preserve_unconfirmed(conn: AsyncConnection, action: dict) -> None:
    result = action["result"] or {}
    sent = result.get("send_state") == "sent"
    interrupted = {
        "send_state": "sent" if sent else "uncertain",
        "stage": "done",
        "verification": "unknown",
        "verification_reason": "interrupted",
    }
    await conn.execute(
        """
        UPDATE action_attempts
           SET status = 'unknown', result = COALESCE(result, '{}'::jsonb) || %s::jsonb,
               finished_at = COALESCE(finished_at, now())
         WHERE id = %s
        """,
        (Jsonb(interrupted), action["attempt_id"]),
    )
    await conn.execute(
        "UPDATE action_proposals SET status = 'unknown' WHERE id = %s",
        (action["proposal_id"],),
    )
    if action["followup_id"] is None:
        await conn.execute(
            """
            INSERT INTO action_followups (
                id, proposal_id, attempt_id, cluster, namespace, target_kind, target_name,
                outcome, reason, evidence, status, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'unknown', %s, %s, 'open', now())
            """,
            (
                f"fol-{uuid4().hex}",
                action["proposal_id"],
                action["attempt_id"],
                action["cluster"],
                action["namespace"],
                action["target_kind"],
                action["target_name"],
                "写入后验证中断，请核实实际资源与业务" if sent else "写入结果未明，请人工核实",
                Jsonb(interrupted),
            ),
        )
    key = "/".join(
        action[field] for field in ("cluster", "namespace", "target_kind", "target_name")
    )
    await conn.execute(
        "INSERT INTO action_locks (target_key, proposal_id) VALUES (%s, %s) "
        "ON CONFLICT (target_key) DO NOTHING",
        (key, action["proposal_id"]),
    )


async def close_conversation(
    store: Store,
    conn: AsyncConnection,
    conversation: dict,
    *,
    fallback: str | None = None,
    interrupted: bool = False,
    action: dict | None = None,
) -> None:
    """调用方须先锁任务行；从锁内事实收尾，保留有待办的执行锁。"""
    conv_id, inv_id = conversation["id"], conversation["investigation_id"]
    cur = await conn.execute(
        "SELECT finished_at FROM conversations WHERE id = %s FOR UPDATE", (conv_id,)
    )
    if (await cur.fetchone())["finished_at"] is not None:
        return
    if action is None:
        action = await _latest_action(conn, inv_id)
    if _unconfirmed(action):
        await _preserve_unconfirmed(conn, action)
    if fallback is not None:
        await conn.execute(
            """
            UPDATE investigations
               SET status = 'aborted', termination_reason = %s, finished_at = now()
             WHERE id = %s AND finished_at IS NULL
            """,
            (fallback, inv_id),
        )
    await conn.execute(
        """
        UPDATE investigation_tasks
           SET phase = 'terminal', outcome = COALESCE(outcome, %s),
               termination_code = COALESCE(termination_code, %s),
               finished_at = COALESCE(finished_at, now()),
               ready_proposal_id = NULL, lease_owner = NULL, lease_expires_at = NULL,
               updated_at = now()
         WHERE investigation_id = %s
        """,
        (
            "needs_human" if _unconfirmed(action) else "aborted",
            "interrupted" if interrupted else "runtime_error",
            inv_id,
        ),
    )
    await conn.execute(
        "UPDATE web_work SET state = 'done', finished_at = now() "
        "WHERE conversation_id = %s AND state <> 'done'",
        (conv_id,),
    )
    await conn.execute(
        """
        WITH revoked AS (
            UPDATE action_proposals p SET status = 'rejected'
             WHERE p.investigation_id = %s AND p.status IN ('approved', 'pending_approval')
               AND NOT EXISTS (SELECT 1 FROM action_attempts a WHERE a.proposal_id = p.id)
            RETURNING p.id
        )
        DELETE FROM action_locks WHERE proposal_id IN (SELECT id FROM revoked)
        """,
        (inv_id,),
    )
    await conn.execute(
        "UPDATE conversation_messages SET delivery = 'unconfirmed' "
        "WHERE conversation_id = %s AND delivery = 'received'",
        (conv_id,),
    )
    cur = await conn.execute(
        "SELECT conclusion, termination_reason FROM investigations WHERE id = %s",
        (inv_id,),
    )
    report = await cur.fetchone()
    resolution = (
        action["resolution"] if action and action["followup_status"] == "resolved" else None
    )
    text = (
        (f"核实结果：{resolution}。调查在处置后结束。" if resolution else None)
        or fallback
        or report["conclusion"]
        or report["termination_reason"]
        or "调查已结束"
    )
    await store._insert_conversation_message(
        conn, conv_id, f"msg-{uuid4().hex}", "conclusion", text
    )
    await conn.execute(
        "UPDATE conversations SET finished_at = now(), interrupted = %s WHERE id = %s",
        (interrupted, conv_id),
    )
    await store._insert_conversation_event(conn, conv_id, "finished")


async def recover_conversation(
    store: Store,
    conversation_id: str,
    instance_id: str,
    *,
    active: bool = False,
) -> str:
    """按恢复表从上往下检查一次。启动与定期检查均不调用图。"""
    if active:
        return "active"
    async with store._pool.connection() as conn:
        conversation = await _lock_conversation(conn, conversation_id)
        if conversation is None or conversation["finished_at"] is not None:
            return "finished"
        if conversation["lease_owner"] == instance_id and conversation["live_lease"]:
            return "active"

        action = await _latest_action(conn, conversation["investigation_id"])
        if conversation["phase"] == "terminal":
            await close_conversation(store, conn, conversation, action=action)
            return "terminal"
        if _unconfirmed(action):
            await close_conversation(
                store,
                conn,
                conversation,
                fallback="处置结果待核实",
                action=action,
            )
            return "needs_verification"
        if action is not None and action["attempt_id"]:
            result = action["result"] or {}
            resolved = action["followup_status"] == "resolved"
            if (
                resolved
                or result.get("verification_done") is True
                or result.get("send_state") == "not_sent"
            ):
                reason = (
                    f"核实结果：{action['resolution']}。调查在处置后结束。"
                    if resolved
                    else _recorded_outcome(result)
                )
                await close_conversation(store, conn, conversation, fallback=reason, action=action)
                return "closed_after_action"

        cur = await conn.execute(
            "SELECT 1 FROM web_work WHERE conversation_id = %s AND state = 'queued'",
            (conversation_id,),
        )
        if await cur.fetchone():
            if conversation["lease_owner"] is not None:
                await conn.execute(
                    "UPDATE investigation_tasks SET lease_owner = NULL, lease_expires_at = NULL "
                    "WHERE investigation_id = %s",
                    (conversation["investigation_id"],),
                )
            return "queued"
        if (
            action is not None
            and conversation["ready_proposal_id"] == action["proposal_id"]
            and action["proposal_status"] == "pending_approval"
        ):
            return "waiting"

        await close_conversation(
            store,
            conn,
            conversation,
            fallback="调查已中断，请开始新调查",
            interrupted=True,
            action=action,
        )
        return "interrupted"
