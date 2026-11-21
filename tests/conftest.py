"""真实库测试的共享 fixture：每次运行登记 ID，结束后只清理本用例创建的行。"""

import shutil
import subprocess
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from db_env import DSN, TEST_REASON

from aiops.config import PROJECT_ROOT


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
            _delete_session(conn, session_id)


def _delete_session(conn, session_id):
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
        _delete_investigation(conn, values)


def _delete_investigation(conn, values):
    # 新运行时的表要先删：它们对 hypotheses / investigations / proposals 有外键。
    conn.execute(
        "DELETE FROM tool_requests WHERE investigation_id = %s", (values["investigation"],)
    )
    conn.execute(
        "DELETE FROM hypothesis_evidence WHERE investigation_id = %s",
        (values["investigation"],),
    )
    conn.execute("DELETE FROM observations WHERE investigation_id = %s", (values["investigation"],))
    conn.execute("DELETE FROM delegations WHERE investigation_id = %s", (values["investigation"],))
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
        conn.execute("DELETE FROM audit_events WHERE correlation_id = %s", (proposal_id,))
        conn.execute("DELETE FROM action_proposals WHERE id = %s", (proposal_id,))
    conn.execute(
        "DELETE FROM investigation_tasks WHERE investigation_id = %s",
        (values["investigation"],),
    )
    conn.execute(
        "DELETE FROM audit_events WHERE correlation_id = ANY(%s)",
        ([values["correlation"], values["investigation"]],),
    )
    conn.execute("DELETE FROM hypotheses WHERE investigation_id = %s", (values["investigation"],))
    conn.execute("DELETE FROM investigations WHERE id = %s", (values["investigation"],))
    conn.execute("DELETE FROM incidents WHERE id = %s", (values["incident"],))
    for table in ("checkpoints", "checkpoint_writes", "checkpoint_blobs"):
        # 取消或租约冲突可能发生在初始化 checkpoint 之前。
        if conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] is not None:
            conn.execute(f"DELETE FROM {table} WHERE thread_id = %s", (values["investigation"],))


@pytest.fixture
def action_session_ids():
    """只清理由动作会话用例登记的会话及其调查。"""
    session_ids: list[str] = []
    yield session_ids
    if not session_ids:
        return
    with psycopg.connect(DSN) as conn:
        for session_id in session_ids:
            investigations = conn.execute(
                """
                SELECT i.id, i.incident_id FROM investigations i
                JOIN session_turns t ON t.id = i.session_turn_id
                WHERE t.session_id = %s
                """,
                (session_id,),
            ).fetchall()
            for investigation_id, incident_id in investigations:
                _delete_investigation(
                    conn,
                    {
                        "investigation": investigation_id,
                        "incident": incident_id,
                        "correlation": investigation_id,
                    },
                )
            _delete_session(conn, session_id)


@pytest.fixture
def isolated_serve_dsn():
    """调度器扫描整个 schema，每条服务测试使用独立账本。"""
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    if not DSN:
        pytest.skip("未配置 VIGIL_DATABASE_URL")
    schema = f"serve_{uuid4().hex}"
    with psycopg.connect(DSN, autocommit=True) as administer:
        administer.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            yield make_conninfo(DSN, options=f"-csearch_path={schema}")
        finally:
            administer.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture(scope="session")
def built_wheel(tmp_path_factory):
    """消费已构建的前端产物，经 sdist 共用一次普通 wheel 构建。"""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("未安装 uv，跳过 wheel 验证")
    directory = tmp_path_factory.mktemp("wheel")
    result = subprocess.run(
        [uv, "build", "--out-dir", str(directory)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        timeout=45,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    (wheel,) = directory.glob("*.whl")
    return wheel
