"""真实库测试的共享 fixture：每次运行登记 ID，结束后只清理本用例创建的行。"""

from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from db_env import DSN, TEST_REASON


@pytest.fixture
def cleanup():
    ids: list[str] = []
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
            conn.execute("DELETE FROM action_followups WHERE proposal_id = %s", (proposal_id,))
            conn.execute("DELETE FROM action_locks WHERE proposal_id = %s", (proposal_id,))
            conn.execute("DELETE FROM action_attempts WHERE proposal_id = %s", (proposal_id,))
            conn.execute("DELETE FROM approvals WHERE proposal_id = %s", (proposal_id,))
            conn.execute("DELETE FROM audit_events WHERE correlation_id = %s", (proposal_id,))
            conn.execute("DELETE FROM action_proposals WHERE id = %s", (proposal_id,))


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
        conn.execute("DELETE FROM audit_events WHERE correlation_id = %s", (values["correlation"],))
        conn.execute(
            "DELETE FROM audit_events WHERE correlation_id = %s", (values["investigation"],)
        )
        conn.execute("DELETE FROM evidence WHERE investigation_id = %s", (values["investigation"],))
        conn.execute(
            "DELETE FROM tool_calls WHERE investigation_id = %s", (values["investigation"],)
        )
        conn.execute(
            "DELETE FROM hypotheses WHERE investigation_id = %s", (values["investigation"],)
        )
        conn.execute("DELETE FROM investigations WHERE id = %s", (values["investigation"],))
        conn.execute("DELETE FROM incidents WHERE id = %s", (values["incident"],))
        conn.execute("DELETE FROM checkpoints WHERE thread_id = %s", (values["investigation"],))
        conn.execute(
            "DELETE FROM checkpoint_writes WHERE thread_id = %s", (values["investigation"],)
        )
