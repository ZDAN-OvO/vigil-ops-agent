"""真实库测试的共享 fixture：每次运行登记 ID，结束后只清理本用例创建的行。"""

from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from db_env import DSN, TEST_REASON


class ActionCleanup(list[str]):
    def __init__(self):
        super().__init__()
        self.sessions: list[str] = []


@pytest.fixture
def cleanup():
    ids = ActionCleanup()
    started_at = datetime.now(UTC)
    yield ids
    with psycopg.connect(DSN) as conn:
        # 失败路径可能来不及登记提案 ID；实验命名空间里本用例开始后的提案都属于测试。
        rows = conn.execute(
            """
            SELECT id FROM action_proposals
             WHERE namespace = %s AND created_at >= %s AND reason = %s
            """,
            ("vigil-experiment", started_at, TEST_REASON),
        ).fetchall()
        for proposal_id in {row[0] for row in rows} | set(ids):
            conn.execute("DELETE FROM investigation_tasks WHERE proposal_id = %s", (proposal_id,))
            conn.execute("DELETE FROM action_followups WHERE proposal_id = %s", (proposal_id,))
            conn.execute("DELETE FROM action_locks WHERE proposal_id = %s", (proposal_id,))
            conn.execute("DELETE FROM tool_requests WHERE proposal_id = %s", (proposal_id,))
            conn.execute("DELETE FROM action_attempts WHERE proposal_id = %s", (proposal_id,))
            conn.execute("DELETE FROM approvals WHERE proposal_id = %s", (proposal_id,))
            conn.execute("DELETE FROM audit_events WHERE correlation_id = %s", (proposal_id,))
            conn.execute("DELETE FROM action_proposals WHERE id = %s", (proposal_id,))

        for session_id in ids.sessions:
            for table in ("checkpoints", "checkpoint_writes", "checkpoint_blobs"):
                conn.execute(f"DELETE FROM {table} WHERE thread_id = %s", (session_id,))
            conn.execute(
                "DELETE FROM session_events WHERE session_id = %s AND turn_id IS NOT NULL",
                (session_id,),
            )
            conn.execute(
                "UPDATE session_inputs SET turn_id = NULL, checkpoint_id = NULL, "
                "delivered_step = NULL "
                "WHERE session_id = %s",
                (session_id,),
            )
            conn.execute("DELETE FROM session_turns WHERE session_id = %s", (session_id,))
            conn.execute("DELETE FROM session_inputs WHERE session_id = %s", (session_id,))
            conn.execute("DELETE FROM session_events WHERE session_id = %s", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = %s", (session_id,))


@pytest.fixture
def ids():
    tag = uuid4().hex[:12]
    values = {
        "incident": f"inc-{tag}",
        "investigation": f"inv-{tag}",
        "correlation": f"corr-{tag}",
    }
    yield values
    with psycopg.connect(DSN) as conn:
        # 新运行时的表要先删：它们对 hypotheses / investigations / proposals 有外键。
        conn.execute(
            "DELETE FROM tool_requests WHERE investigation_id = %s", (values["investigation"],)
        )
        conn.execute(
            "DELETE FROM hypothesis_evidence WHERE investigation_id = %s",
            (values["investigation"],),
        )
        conn.execute(
            "DELETE FROM observations WHERE investigation_id = %s", (values["investigation"],)
        )
        conn.execute(
            "DELETE FROM delegations WHERE investigation_id = %s", (values["investigation"],)
        )
        for proposal_id in [
            row[0]
            for row in conn.execute(
                "SELECT id FROM action_proposals WHERE investigation_id = %s",
                (values["investigation"],),
            ).fetchall()
        ]:
            conn.execute("DELETE FROM action_followups WHERE proposal_id = %s", (proposal_id,))
            conn.execute("DELETE FROM tool_requests WHERE proposal_id = %s", (proposal_id,))
            conn.execute("DELETE FROM investigation_tasks WHERE proposal_id = %s", (proposal_id,))
            conn.execute("DELETE FROM action_attempts WHERE proposal_id = %s", (proposal_id,))
            conn.execute("DELETE FROM approvals WHERE proposal_id = %s", (proposal_id,))
            conn.execute("DELETE FROM action_locks WHERE proposal_id = %s", (proposal_id,))
            conn.execute("DELETE FROM action_proposals WHERE id = %s", (proposal_id,))
        conn.execute(
            "DELETE FROM investigation_tasks WHERE investigation_id = %s",
            (values["investigation"],),
        )
        conn.execute("DELETE FROM audit_events WHERE correlation_id = %s", (values["correlation"],))
        conn.execute(
            "DELETE FROM audit_events WHERE correlation_id = %s", (values["investigation"],)
        )
        conn.execute(
            "DELETE FROM hypotheses WHERE investigation_id = %s", (values["investigation"],)
        )
        conn.execute("DELETE FROM investigations WHERE id = %s", (values["investigation"],))
        conn.execute("DELETE FROM incidents WHERE id = %s", (values["incident"],))
        for table in ("checkpoints", "checkpoint_writes", "checkpoint_blobs"):
            # 取消或租约冲突可能发生在初始化 checkpoint 之前。
            if conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] is not None:
                conn.execute(
                    f"DELETE FROM {table} WHERE thread_id = %s", (values["investigation"],)
                )
