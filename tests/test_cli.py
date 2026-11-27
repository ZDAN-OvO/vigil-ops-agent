"""通过子进程、伪终端和隔离 PostgreSQL 验证公开 CLI。"""

import asyncio
import errno
import json
import os
import pty
import re
import select
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import ModuleType
from uuid import uuid4

import psycopg
import pytest
from actions_support import legacy_risk, make_proposal
from controlled_action_support import REPLICA_TARGET, ReplicaAdapter, drive_replica, replica_runtime
from investigation_support import (
    FakePrometheus,
    FakeRestartAdapter,
    FakeRolloutAdapter,
    FakeToolbox,
    ScriptedChatModel,
    call,
    create_hypothesis,
    hypothesis_id,
    observation_ids,
    report_call,
)
from langchain_core.messages import AIMessage
from psycopg import sql
from psycopg.conninfo import make_conninfo

from aiops import cli
from aiops.actions.executor import ActionExecutor
from aiops.actions.policy import target_key
from aiops.actions.rollout_undo import ActionError
from aiops.actions.safety import ActionSafety
from aiops.agent.conversation import ConversationDriver
from aiops.config import PROJECT_ROOT, Settings
from aiops.contracts import ActionAttempt, ApprovalDecision, AttemptStatus, ProposalStatus
from aiops.db import Store
from aiops.investigation import factory
from aiops.sessions.store import SessionStore

CHILD = [sys.executable, str(Path(__file__).resolve())]
PRIVATE = "sk-cli-hidden-example /home/cli/private.txt"
READY = "正在查询"
ANSWER = "当前有 2 个副本"
SIDE = "这是只读旁路的回复"
FINISHED = "处置轮次继续完成"
WEB_URL = "http://127.0.0.1:8123"
DEPLOYMENTS = call("kubernetes_list_deployments", {"selector": "app=checkout"}, "deployments")
ASSESS = "assess_evidence"
REPLICA_PROBE = 'probe_success{job="vigil-replica-probe"}'


@pytest.fixture
def isolated_db():
    dsn = os.environ.get("VIGIL_DATABASE_URL")
    if not dsn:
        pytest.skip("未显式配置 VIGIL_DATABASE_URL，跳过 PostgreSQL 集成测试")
    schema = sql.Identifier("cli_" + uuid4().hex)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
        try:
            yield make_conninfo(dsn, options=f"-c search_path={schema.as_string(conn)}")
        finally:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(schema))


@pytest.fixture
def child_env(tmp_path, isolated_db):
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "LANG": "C.UTF-8",
        "VIGIL_DATABASE_URL": isolated_db,
        "VIGIL_MODEL_NAME": "cli-test-model",
        "VIGIL_RUNTIME_DIR": str(tmp_path / "runtime"),
        "VIGIL_ACTION_VERIFY_SECONDS": "6",
        "VIGIL_ACTION_SAMPLE_SECONDS": "1",
        "VIGIL_ACTION_ROLLOUT_TIMEOUT_SECONDS": "5",
        "CLI_TEST_MODE": "simple",
        "CLI_TEST_FILES": str(tmp_path),
    }


def _run(env, *args, stdin="", command=CHILD):
    return subprocess.run(
        [*command, *args],
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        cwd=env["HOME"],
        timeout=25,
        check=False,
    )


def _stream(proc, code):
    assert proc.returncode == code, (proc.stdout, proc.stderr)
    events = [json.loads(line) for line in proc.stdout.splitlines()]
    assert [event["type"] for event in events].count("result") == 1
    assert events[-1]["type"] == "result" and events[-1]["exit_code"] == code
    assert all({"type", "session_id", "seq", "ts"} <= event.keys() for event in events)
    sequences = [event["seq"] for event in events if event["seq"] is not None]
    assert sequences == sorted(set(sequences))
    assert all(value not in proc.stdout + proc.stderr for value in PRIVATE.split())
    return events


def _sql(env, query, *params):
    with psycopg.connect(env["VIGIL_DATABASE_URL"]) as conn:
        cursor = conn.execute(query, params)
        return cursor.fetchall() if cursor.description is not None else None


class Terminal:
    def __init__(self, process, master):
        self.process = process
        self.master = master
        self.output = b""

    def send(self, line):
        os.write(self.master, (line + "\n").encode())

    def until(self, value):
        expected = value.encode()
        deadline = time.monotonic() + 20
        while expected not in self.output:
            delay = max(0, deadline - time.monotonic())
            if not select.select([self.master], [], [], delay)[0] or not self.read():
                pytest.fail(f"终端未输出 {value!r}，已收到 {self.text!r}")

    def read(self):
        try:
            block = os.read(self.master, 64 * 1024)
        except OSError as exc:
            if exc.errno == errno.EIO:
                return False
            raise
        self.output += block
        return bool(block)

    @property
    def text(self):
        return self.output.decode(errors="replace")

    def finish(self, code):
        deadline = time.monotonic() + 25
        while self.process.poll() is None or select.select([self.master], [], [], 0)[0]:
            delay = max(0, deadline - time.monotonic())
            if not select.select([self.master], [], [], delay)[0] or not self.read():
                break
        assert self.process.wait(timeout=2) == code, self.text


@contextmanager
def _pty(env, *args) -> Iterator[Terminal]:
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            [*CHILD, *args], stdin=slave, stdout=slave, stderr=slave, env=env, cwd=env["HOME"]
        )
    finally:
        os.close(slave)
    try:
        yield Terminal(proc, master)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        os.close(master)


def test_usage_failures_and_local_result(tmp_path):
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "LANG": "C.UTF-8"}
    for args in [(), ("-p", ""), ("--resume", "x", "-c"), ("admin", "approve", "id")]:
        proc = _run(env, *args)
        assert proc.returncode == 2 and not proc.stdout and "usage:" in proc.stderr
    failed = _run(env, "-p", "检查", "--output", "stream-json")
    (result,) = _stream(failed, 1)
    assert result["seq"] is None and result["persisted"] is False
    assert "VIGIL_DATABASE_URL" in failed.stderr
    assert _run(env, "admin", "reconcile-action", "id").returncode == 2


def test_once_streams_during_work_and_keeps_real_sequence(child_env, tmp_path):
    env = child_env | {"CLI_TEST_MODE": "blocking"}
    with _pty(env, "-p", "检查 " + PRIVATE, "--output", "stream-json") as term:
        term.until('"status": "running"')
        assert '"type": "result"' not in term.text
        (tmp_path / "continue").touch()
        term.finish(0)
    events = [json.loads(line) for line in term.text.splitlines()]
    result = events[-1]
    assert result["summary"].startswith(ANSWER)
    assert all(secret not in term.text for secret in PRIVATE.split())
    session_id = result["session_id"]
    expected = _sql(
        env, "SELECT seq, type FROM session_events WHERE session_id = %s ORDER BY seq", session_id
    )
    assert all((event["seq"], event["type"]) in expected for event in events)
    assert [event["seq"] for event in events] == [seq for seq, _ in expected]
    assert _sql(env, "SELECT lease_owner FROM sessions WHERE id = %s", session_id) == [(None,)]
    resumed = _stream(_run(child_env, "-c", "--output", "stream-json"), 0)
    marker = resumed[0]
    assert marker["session_id"] == session_id and marker["resumed"] is True
    assert marker["seq"] is None and marker["persisted"] is False
    assert resumed[-1] == result
    formatted = _run(child_env, "-p", "再检查", "--resume", session_id, "--output", "json")
    payload = json.loads(formatted.stdout)
    assert formatted.returncode == 0 and payload["exit_code"] == 0 and payload["cards"] == []
    assert len(formatted.stdout.splitlines()) == 1

    async def seed_alert():
        async with Store.open(env["VIGIL_DATABASE_URL"]) as store:
            sessions = SessionStore(store)
            async with sessions.alert_transaction("cli-alert-group") as conn:
                return await sessions.record_alert(
                    conn,
                    "cli-alert-group",
                    title="告警会话",
                    text="告警",
                    display_text="告警",
                    repeat_display_text="再次告警",
                )

    alert = asyncio.run(seed_alert())
    result = _run(child_env, "sessions")
    assert result.returncode == 0, result.stderr
    assert f"{session_id}\tuser\t" in result.stdout
    assert f"{alert.session_id}\talert\t" in result.stdout


def test_budget_failure_and_model_stderr_are_projected(child_env):
    budget = child_env | {"VIGIL_INVESTIGATION_BUDGET_MODEL_CALLS": "1"}
    exhausted = _stream(_run(budget, "-p", "检查", "--output", "stream-json"), 4)[-1]
    assert exhausted["termination_code"] == "budget_exhausted"
    error = _run(child_env | {"CLI_TEST_MODE": "failing"}, "-p", "出错", "--output", "stream-json")
    result = _stream(error, 1)[-1]
    assert result["seq"] is not None
    assert "Traceback" not in error.stderr


def _card_run(env, output="stream-json"):
    paused = _run(env | {"CLI_TEST_MODE": "card"}, "-p", "提出卡片", "--output", output)
    if output == "json":
        assert paused.returncode == 3, (paused.stdout, paused.stderr)
        result = json.loads(paused.stdout)
        assert len(paused.stdout.splitlines()) == 1
        (card,) = result["cards"]
    else:
        events = _stream(paused, 3)
        (card,) = [event for event in events if event["type"] == "approval_required"]
        result = events[-1]
    assert result["card_id"] == card["card_id"]
    assert card["action"] == "scale_replicas"
    assert card["before"] == {"replicas": 1}
    assert card["parameters"] == {"replicas": 2}
    assert card["url"] == f"{WEB_URL}/sessions/{card['session_id']}#card-{card['card_id']}"
    return card


def _decide(env, card, decision=ApprovalDecision.APPROVED):
    async def run():
        async with Store.open(env["VIGIL_DATABASE_URL"]) as store:
            await ActionSafety(store).record_card_decision(
                card["card_id"],
                session_id=card["session_id"],
                fingerprint=card["fingerprint"],
                decision=decision,
                actor="web-test",
            )

    asyncio.run(run())


def _record_replica_write(fields):
    with (Path(os.environ["CLI_TEST_FILES"]) / "writes").open("a") as file:
        print(fields["replicas"], file=file)


def _run_approved_job(env, card_id):
    async def run():
        async with Store.open(env["VIGIL_DATABASE_URL"]) as store:

            def record(fields):
                with (Path(env["CLI_TEST_FILES"]) / "writes").open("a") as file:
                    print(fields["replicas"], file=file)

            runtime = replica_runtime(store, kube=ReplicaAdapter(record_write=record))
            return await drive_replica(runtime, card_id)

    return asyncio.run(run())


def test_paused_once_releases_lease_and_persists_takeover_receipt(child_env, tmp_path):
    card = _card_run(child_env)
    session_id, card_id = card["session_id"], card["card_id"]
    assert _sql(child_env, "SELECT lease_owner, state FROM sessions") == [
        (None, "awaiting_approval")
    ]
    budget = child_env | {"VIGIL_INVESTIGATION_BUDGET_MODEL_CALLS": "1"}
    side = _stream(
        _run(
            budget,
            "-p",
            "旁路查询",
            "--resume",
            session_id,
            "--output",
            "stream-json",
        ),
        3,
    )[-1]
    assert side["termination_code"] == "budget_exhausted" and side["lane"] == "side"
    assert _sql(child_env, "SELECT count(*) FROM approvals") == [(0,)]
    _decide(child_env, card)
    assert not (tmp_path / "writes").exists()
    assert _run_approved_job(child_env, card_id).status is AttemptStatus.VERIFIED
    env = child_env | {"CLI_TEST_MODE": "card"}
    args = ("--resume", session_id, "--output", "stream-json")
    takeover = _stream(_run(env, *args), 0)
    replay = _stream(_run(env, *args), 0)
    assert takeover[-1] == replay[-1]
    receipts = [
        event for event in replay if event["type"] == "action_result" and event["written"] is True
    ]
    assert len(receipts) == 2 and all(event["card_id"] == card_id for event in receipts)
    assert receipts[-1]["status"] == "verified"
    assert (tmp_path / "writes").read_text().splitlines() == ["2"]


def test_side_json_waits_for_its_own_input_and_keeps_main_card(child_env):
    card = _card_run(child_env)
    session_id = card["session_id"]

    async def seed_interrupted_side():
        async with Store.open(child_env["VIGIL_DATABASE_URL"]) as store:
            sessions = SessionStore(store)
            await sessions.enqueue_input(session_id, "旧问题", display_text="旧问题")
            lease = await sessions.acquire_side_lease(session_id, "finished-test-process")
            assert lease is not None
            await sessions.release_lease(lease)

    asyncio.run(seed_interrupted_side())
    env = child_env | {"CLI_TEST_MODE": "card"}
    proc = _run(env, "-p", "本次提问", "--resume", session_id, "--output", "json")
    assert proc.returncode == 3, proc.stderr
    result = json.loads(proc.stdout)
    assert result["side_question"] == "本次提问"
    assert result["card_id"] == card["card_id"]
    assert result["cards"] == [card]
    assert _sql(child_env, "SELECT count(*) FROM session_inputs WHERE checkpoint_id IS NULL") == [
        (0,)
    ]


def test_side_takeover_uses_bound_turn_before_delivery(child_env, tmp_path, monkeypatch):
    card = _card_run(child_env)
    session_id = card["session_id"]

    async def run():
        async with Store.open(child_env["VIGIL_DATABASE_URL"]) as store:
            driver = ConversationDriver(
                settings=Settings(
                    _env_file=None,
                    model_name="cli-test-model",
                    runtime_dir=tmp_path,
                    investigation_budget_seconds=5,
                    investigation_budget_action_seconds=1,
                ),
                store=store,
                toolbox=FakeToolbox(),
                model=ScriptedChatModel(responses=[AIMessage(content=SIDE)]),
            )
            sessions = driver.sessions
            enqueue = sessions.enqueue_input

            async def bind_then_finish_main(*args, **kwargs):
                incoming = await enqueue(*args, **kwargs)
                side = await sessions.acquire_side_lease(session_id, "interrupted-side")
                assert side is not None
                await sessions.release_lease(side)
                main = await sessions.acquire_main_lease(session_id, "finished-main")
                assert main is not None
                turn = await sessions.start_main_turn(main)
                async with sessions.run(main, turn.id) as tx:
                    await tx.set_turn_state("completed")
                await sessions.release_lease(main)
                assert (await sessions.get_session(session_id)).state == "idle"
                assert (await sessions.get_input(incoming.id)).checkpoint_id is None
                return incoming

            monkeypatch.setattr(sessions, "enqueue_input", bind_then_finish_main)
            outcome = await cli._drive_side_input(
                driver, session_id, "主线完成后的旁路接手", cli.OnceOutput("json")
            )
            assert outcome.exit_code == 3
            assert outcome.result.payload["side_question"] == "主线完成后的旁路接手"

    asyncio.run(run())


def test_closed_resume_reports_closure_after_historical_card(child_env):
    card = _card_run(child_env)
    session_id = card["session_id"]

    async def close():
        async with Store.open(child_env["VIGIL_DATABASE_URL"]) as store:
            async with store.transaction() as conn:
                await ActionSafety(store).close_session(conn, session_id, reason="预演关闭")

    asyncio.run(close())
    events = _stream(_run(child_env, "--resume", session_id, "--output", "stream-json"), 0)
    assert any(event["type"] == "session_closed" for event in events[:-1])
    assert events[-1]["status"] == "closed"
    assert events[-1]["seq"] is None


def test_pipe_and_machine_modes_never_approve_or_reject(child_env):
    card = _card_run(child_env, "json")
    session_id, card_id = card["session_id"], card["card_id"]
    prefix = card_id.removeprefix("act-")[:4]
    env = child_env | {"CLI_TEST_MODE": "card"}

    # 管道输入对文本恢复不提供审批读口。
    pipe = _run(env, "--resume", session_id, stdin=f"/approve {prefix}\n")
    assert pipe.returncode == 3 and f"批准 /approve {prefix}" not in pipe.stdout

    # stdout 重定向时，即使 stdin 是终端也禁止决定。
    master, slave = pty.openpty()
    try:
        with subprocess.Popen(
            [*CHILD, "--resume", session_id],
            env=env,
            cwd=env["HOME"],
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as proc:
            os.write(master, f"/reject {prefix}\n".encode())
            output, error = proc.communicate(timeout=20)
            assert proc.returncode == 3, error
            assert b"/reject" not in output
    finally:
        os.close(slave)
        os.close(master)

    # 两端为终端也不能给 -p 或机器输出模式打开操作口。
    for args, injected in [
        (("--resume", session_id, "--output", "stream-json"), f"/approve {prefix}"),
        (("-p", "旁路问题", "--resume", session_id), f"/reject {prefix}"),
    ]:
        with _pty(env, *args) as term:
            term.send(injected)
            term.finish(3)
            assert f"批准 /approve {prefix}" not in term.text
    assert _sql(env, "SELECT count(*) FROM approvals WHERE proposal_id = %s", card_id) == [(0,)]
    assert _sql(env, "SELECT lease_owner, state FROM sessions WHERE id = %s", session_id) == [
        (None, "awaiting_approval")
    ]


@pytest.mark.parametrize("source", ["terminal", "web", "reject"])
def test_terminal_holds_main_for_side_questions_and_resumes_on_decision(
    child_env, tmp_path, source
):
    env = child_env | {"CLI_TEST_MODE": "card"}
    with _pty(env, "提出卡片") as term:
        term.until("拒绝 /reject ")
        match = re.search(r"批准 /approve ([a-f0-9]{4})", term.text)
        assert match is not None, term.text
        prefix = match[1]
        [(session_id, card_id, owner)] = _sql(
            env,
            "SELECT s.id, p.id, s.lease_owner FROM action_proposals p "
            "JOIN sessions s ON p.session_id = s.id",
        )
        assert owner is not None and card_id.startswith(f"act-{prefix}")
        wrong = "0000" if prefix != "0000" else "0001"
        term.send(f"/approve {wrong}")
        term.until("四位前缀")
        term.send("批准")
        term.until(SIDE)
        assert _sql(env, "SELECT count(*) FROM approvals") == [(0,)]
        side = _stream(
            _run(
                env,
                "--resume",
                session_id,
                "-p",
                "旁路问题",
                "--output",
                "stream-json",
            ),
            3,
        )
        assert side[-1]["summary"] == SIDE and side[-1]["lane"] == "side"
        assert any(event.get("card_id") == card_id for event in side)
        assert _sql(env, "SELECT lease_owner FROM sessions WHERE id = %s", session_id) == [(owner,)]
        if source == "web":
            [event] = _sql(
                env, "SELECT payload FROM session_events WHERE type = 'approval_required'"
            )
            _decide(env, event[0] | {"session_id": session_id})
        else:
            term.send(f"/{'reject' if source == 'reject' else 'approve'} {prefix}")
        term.until(FINISHED)
        term.send("/exit")
        term.finish(0)

    replay = _stream(_run(env, "--resume", session_id, "--output", "stream-json"), 0)
    results = [event for event in replay if event["type"] == "action_result"]
    if source == "reject":
        assert not (tmp_path / "writes").exists()
        assert _sql(env, "SELECT decision FROM approvals") == [("rejected",)]
    else:
        assert (tmp_path / "writes").read_text().splitlines() == ["2"]
        assert _sql(env, "SELECT decision FROM approvals") == [("approved",)]
        assert any(result["card_id"] == card_id and result["written"] for result in results)
    assert _sql(env, "SELECT lease_owner FROM sessions WHERE id = %s", session_id) == [(None,)]


def test_interjection_is_saved_while_query_in_flight(child_env, tmp_path):
    env = child_env | {"CLI_TEST_MODE": "blocking"}
    with _pty(env, "检查") as term:
        term.until(READY)
        term.send("查询过程中的线索")
        term.until("你> 查询过程中的线索")
        assert not (tmp_path / "continue").exists()
        (tmp_path / "continue").touch()
        term.until(ANSWER)
        term.send("/exit")
        term.finish(0)
    assert _sql(
        env,
        "SELECT delivered_step IS NOT NULL FROM session_inputs WHERE text = %s",
        "查询过程中的线索",
    ) == [(True,)]


def test_serve_requires_both_terminal_ends(tmp_path):
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "LANG": "C.UTF-8",
        "CLI_TEST_SERVE": "1",
    }
    assert _run(env, "serve").stdout.strip() == "false"
    with _pty(env, "serve") as term:
        term.finish(0)
        assert term.text.strip() == "true"
    master, slave = pty.openpty()
    try:
        for input_tty in (True, False):
            with subprocess.Popen(
                [*CHILD, "serve"],
                env=env,
                cwd=env["HOME"],
                stdin=slave if input_tty else subprocess.PIPE,
                stdout=subprocess.PIPE if input_tty else slave,
                stderr=subprocess.PIPE,
            ) as proc:
                output, error = proc.communicate(timeout=15)
                assert proc.returncode == 0, error
                if input_tty:
                    assert output.strip() == b"false"
                else:
                    assert os.read(master, 1024).strip() == b"false"
    finally:
        os.close(slave)
        os.close(master)


def _legacy_proposal(env, *, unknown=False, owner=None):
    async def seed():
        proposal = make_proposal(
            status=ProposalStatus.UNKNOWN if unknown else ProposalStatus.PENDING_APPROVAL
        )
        async with Store.open(env["VIGIL_DATABASE_URL"]) as store:
            await store.migrate()
            await store.save_proposal(proposal, legacy_risk(proposal))
            if unknown:
                await store.save_attempt(
                    ActionAttempt(
                        id="attempt-cli",
                        proposal_id=proposal.id,
                        status=AttemptStatus.UNKNOWN,
                        idempotency_key=proposal.idempotency_key,
                        request={},
                        snapshot={"uid": proposal.target.uid, "resource_version": "10"},
                        started_at=datetime.now(UTC),
                    )
                )
            if owner:
                target = proposal.target
                key = target_key(target.cluster, target.namespace, target.kind, target.name)
                assert await store.acquire_lock(key, proposal.id)
        return proposal.id

    proposal_id = asyncio.run(seed())
    if owner:
        _sql(
            env,
            "UPDATE action_locks SET owner = %s, generation = 1, "
            "session_owner = %s, session_generation = 1, "
            "expires_at = clock_timestamp() + interval '2 minutes' WHERE proposal_id = %s",
            owner,
            owner,
            proposal_id,
        )
    return proposal_id


def test_admin_migrate_runbook_doctor_and_cutover(child_env, tmp_path):
    migration = _run(child_env, "admin", "migrate")
    assert migration.returncode == 0, migration.stderr
    assert {"0013_action_safety", "0016_remediation"} <= set(
        json.loads(migration.stdout)["applied"]
    )
    assert _sql(child_env, "SELECT count(*) FROM checkpoint_migrations")[0][0] > 0
    doctor = _run(child_env, "admin", "doctor")
    assert doctor.returncode == 0, doctor.stderr
    assert json.loads(doctor.stdout)["skills"] >= 5
    runbook = tmp_path / "runbook.json"
    runbook.write_text(
        json.dumps(
            {
                "id": "cli-check",
                "version": 1,
                "title": "检查",
                "summary": "读副本数",
                "steps": ["查看 Deployment"],
                "source": "测试",
            }
        )
    )
    imported = _run(child_env, "admin", "runbook", "import", str(runbook))
    assert imported.returncode == 0, imported.stderr
    assert _sql(child_env, "SELECT id FROM runbooks") == [("cli-check",)]
    card_id = _legacy_proposal(child_env, unknown=True)
    args = ("admin", "prepare-legacy-cutover")
    preview = _run(child_env, *args)
    assert preview.returncode == 0, preview.stderr
    assert json.loads(preview.stdout)["actions"][0]["classification"] == "retain"
    assert _sql(child_env, "SELECT count(*) FROM action_locks") == [(0,)]
    denied = _run(child_env, *args, "--apply", "--confirm", "--old-processes-stopped")
    assert denied.returncode == 1 and "须确认" in denied.stderr
    assert _sql(child_env, "SELECT count(*) FROM action_locks") == [(0,)]
    applied = _run(
        child_env,
        *args,
        "--apply",
        "--confirm",
        "--old-processes-stopped",
        "--restart-disabled",
        "--backup-complete",
    )
    assert applied.returncode == 0, applied.stderr
    assert _sql(child_env, "SELECT proposal_id FROM action_locks") == [(card_id,)]
    followups = _run(child_env, "admin", "followups", "--status", "open", "--proposal-id", card_id)
    assert followups.returncode == 0, followups.stderr
    (followup,) = json.loads(followups.stdout)
    assert followup["proposal_id"] == card_id


def test_reconcile_preserves_lock_until_preparation_and_conclusion_succeed(child_env):
    card_id = _legacy_proposal(child_env, unknown=True, owner="live-sender")
    args = ("admin", "reconcile-action", card_id)
    for mode, flags, expected in [
        ("ok", (), "原发送者仍可能在运行"),
        ("isolation", ("--sender-stopped",), "版本隔离失败"),
        ("read", ("--sender-stopped",), "读取目标失败"),
    ]:
        with _pty(child_env | {"CLI_TEST_RECONCILE": mode}, *args, *flags) as term:
            term.finish(1)
            assert expected in term.text
            assert all(value not in term.text for value in PRIVATE.split())
        assert _sql(child_env, "SELECT proposal_id FROM action_locks") == [(card_id,)]
        assert _sql(child_env, "SELECT status FROM action_attempts") == [("unknown",)]

    env = child_env | {"CLI_TEST_RECONCILE": "ok"}
    with _pty(env, *args, "--sender-stopped") as term:
        term.until("写入情况")
        term.send("未知用法")
        term.finish(2)
    assert _sql(env, "SELECT proposal_id FROM action_locks") == [(card_id,)]
    with _pty(env, *args, "--sender-stopped") as term:
        term.until("写入情况")
        term.send("not-written")
        term.until("核实依据")
        term.send("核对版本及当前模板，确认未写入")
        term.finish(0)
        assert '"written": false' in term.text
    assert _sql(env, "SELECT count(*) FROM action_locks") == [(0,)]
    assert _sql(env, "SELECT status FROM action_attempts") == [("failed",)]


class _ModelGateway(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append(body)
        message = {"role": "assistant", "content": "已读内置 Skill"}
        finish = "stop"
        if len(self.requests) == 1:
            message["content"] = None
            message["tool_calls"] = [
                {
                    "id": "call-skill",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps(
                            {"file_path": "/hypothesis-driven-investigation/SKILL.md"}
                        ),
                    },
                }
            ]
            finish = "tool_calls"
        result = json.dumps(
            {
                "id": "chatcmpl-cli",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": finish,
                    }
                ],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(result)))
        self.end_headers()
        self.wfile.write(result)


def test_wheel_cli_uses_packaged_skills_from_outside_source(child_env, tmp_path, built_wheel):
    uv = shutil.which("uv")
    install = tmp_path / "installed"
    subprocess.run(
        [uv, "pip", "install", "--no-deps", "--target", str(install), str(built_wheel)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        timeout=30,
    )
    command = [sys.executable, str(install / "bin" / "vigil")]
    env = child_env | {"PYTHONPATH": str(install)}
    env.pop("VIGIL_RUNTIME_DIR")
    _ModelGateway.requests = []
    with ThreadingHTTPServer(("127.0.0.1", 0), _ModelGateway) as endpoint:
        server = Thread(target=endpoint.serve_forever, daemon=True)
        server.start()
        try:
            env["VIGIL_MODEL_BASE_URL"] = f"http://127.0.0.1:{endpoint.server_port}/v1"
            result = _stream(
                _run(
                    env,
                    "-p",
                    "读取假设驱动排查 Skill",
                    "--output",
                    "stream-json",
                    command=command,
                ),
                0,
            )[-1]
        finally:
            endpoint.shutdown()
            server.join(timeout=5)
    assert result["summary"] == "已读内置 Skill"
    for request in _ModelGateway.requests:
        assert request["tool_choice"] == "auto"
        assert any(tool["function"]["name"] == "ReportDraft" for tool in request["tools"])
    tool_result = next(
        message["content"]
        for message in _ModelGateway.requests[-1]["messages"]
        if message.get("tool_call_id") == "call-skill"
    )
    assert "从现象提出 1 到 3 个互斥的异常原因假设" in tool_result
    probe = _run(
        env,
        "-c",
        (
            "from aiops.config import Settings; "
            "s = Settings(_env_file=None); print(s.skills_path); print(s.runtime_path)"
        ),
        command=[sys.executable],
    )
    assert probe.returncode == 0, probe.stderr
    skills, runtime = [Path(line) for line in probe.stdout.splitlines()]
    assert skills.is_relative_to(install) and (skills / "oom" / "SKILL.md").is_file()
    assert runtime == tmp_path / ".local" / "state" / "vigil" / "runtime"
    override = _run(
        env | {"VIGIL_SKILLS_DIR": str(PROJECT_ROOT / "skills")},
        "admin",
        "doctor",
        command=command,
    )
    assert override.returncode == 0 and json.loads(override.stdout)["skills"] >= 5


class _ReconcileAdapter:
    def __init__(self, **kwargs):
        self.version = "10"
        self.marker = None
        self.mode = os.environ["CLI_TEST_RECONCILE"]

    def isolate(self, namespace, name, uid, version, token):
        if self.mode == "isolation":
            raise ActionError("isolation_failed", f"模拟版本隔离失败 {PRIVATE}")
        self.version = "11"
        self.marker = token
        return self.version

    def state(self, namespace, name):
        if self.mode == "read":
            raise ActionError("read_failed", f"模拟读取目标失败 {PRIVATE}")
        return {
            "uid": "uid-1",
            "resource_version": self.version,
            "reconciliation_marker": self.marker,
        }


class _BlockingToolbox(FakeToolbox):
    async def run(self, context, time_range, request):
        flag = Path(os.environ["CLI_TEST_FILES"]) / "continue"
        for _ in range(200):
            if flag.exists():
                break
            await asyncio.sleep(0.05)
        return await super().run(context, time_range, request)


class _RestartAdapter(FakeRestartAdapter):
    def __init__(self):
        super().__init__()
        self.tag = "cli-stable"

    def execute(self, plan):
        with (Path(os.environ["CLI_TEST_FILES"]) / "writes").open("a") as file:
            print(plan.uid, file=file)
        return super().execute(plan)


def _assess(messages):
    hyp = hypothesis_id(messages)
    return call(
        ASSESS,
        {
            "links": [
                {
                    "observation_id": obs_id,
                    "hypothesis_id": hyp,
                    "relation": "supporting",
                    "rationale": "平台与业务观测一致",
                }
                for obs_id in observation_ids(messages)[:2]
            ],
            "hypothesis_updates": [
                {
                    "hypothesis_id": hyp,
                    "status": "supported",
                    "reason": "目标与记录一致",
                }
            ],
        },
        "assess",
    )


def _legacy_restart(messages):
    return call(
        "request_action",
        {
            "action": "restart_pod",
            "purpose": "diagnostic",
            "target_pod": "target-pod",
            "service": "target-service",
            "path": "/work",
            "reason": "验证 Pod 级异常",
            "hypothesis_id": hypothesis_id(messages),
            "evidence_ids": observation_ids(messages)[:2],
            "prediction": "重建后业务恢复",
            "absence_meaning": "异常来自模板",
            "business_target": "target-service:/work",
            "observation_window_seconds": 6,
        },
        "request-action",
    )


def _legacy_rollback(messages):
    return call(
        "request_action",
        {
            "action": "rollout_undo",
            "target_deployment": "target-deploy",
            "reason": "核对发布差异",
            "hypothesis_id": hypothesis_id(messages),
            "evidence_ids": observation_ids(messages)[:2],
        },
        "request-action",
    )


def _scale(messages):
    return call(
        "request_action",
        {
            "action": "scale_replicas",
            "target_deployment": REPLICA_TARGET,
            "target_namespace": "vigil-experiment",
            "parameters": {"replicas": 2},
            "reason": "容量不足",
            "hypothesis_id": hypothesis_id(messages),
            "evidence_ids": observation_ids(messages)[:2],
        },
        "request-action",
    )


@pytest.mark.parametrize("action", ["restart_pod", "rollout_undo"])
def test_legacy_request_is_denied_in_stream_json(child_env, tmp_path, action):
    env = child_env | {"CLI_TEST_MODE": action}
    events = _stream(_run(env, "-p", "检查旧写入", "--output", "stream-json"), 0)
    assert not [
        event for event in events if event["type"] in {"approval_required", "action_result"}
    ]
    assert _sql(env, "SELECT kind, status FROM action_proposals") == [(action, "denied")]
    assert _sql(env, "SELECT count(*) FROM approvals") == [(0,)]
    assert _sql(env, "SELECT count(*) FROM action_attempts") == [(0,)]
    assert not (tmp_path / "writes").exists()


class _Model(ScriptedChatModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        mode = os.environ["CLI_TEST_MODE"]
        if mode == "failing":
            cli.logger.error("模拟故障 {} token=synthetic-cli-secret", PRIVATE)
            raise RuntimeError(PRIVATE)
        return super()._generate(messages, stop, run_manager, **kwargs)


def _child_main():
    class TestSettings(Settings):
        web_url: str = WEB_URL
        web_port: int = 8123

    cli.Settings = lambda: TestSettings(_env_file=None)
    if os.environ.get("CLI_TEST_SERVE"):
        serve = ModuleType("aiops.serve")

        async def run_serve(settings, *, interactive):
            print(json.dumps(interactive))

        serve.run_serve = run_serve
        sys.modules[serve.__name__] = serve

    if os.environ.get("CLI_TEST_RECONCILE"):
        cli.RolloutUndoAdapter = _ReconcileAdapter

    if "CLI_TEST_MODE" in os.environ:
        mode = os.environ["CLI_TEST_MODE"]
        toolbox = _BlockingToolbox() if mode == "blocking" else FakeToolbox()

        @asynccontextmanager
        async def open_test_remediation(settings, store):
            yield (
                replica_runtime(store, kube=ReplicaAdapter(record_write=_record_replica_write))
                if mode == "card"
                else None
            )

        async def drive_test_remediation(runtime, stop):
            while not stop.is_set():
                for proposal_id in await runtime.ledger.due():
                    await drive_replica(runtime, proposal_id)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.05)
                except TimeoutError:
                    pass

        cli.open_remediation = open_test_remediation
        cli.drive_remediation = drive_test_remediation

        class TestExecutor(ActionExecutor):
            def __init__(self, **kwargs):
                super().__init__(
                    **(
                        kwargs
                        | {
                            "adapter": FakeRolloutAdapter(),
                            "restart_adapter": _RestartAdapter(),
                            "prometheus": FakePrometheus(),
                            "verify_delay_seconds": 0,
                            "verify_grace_seconds": 0,
                        }
                    )
                )

        factory.ActionExecutor = TestExecutor

        @asynccontextmanager
        async def open_test_conversation(*, settings, store, remediation=None):
            async with factory.open_conversation(
                settings=settings, store=store, remediation=remediation
            ) as driver:
                driver.toolbox = toolbox
                if mode in {"card", "restart_pod", "rollout_undo"}:
                    request = {
                        "card": _scale,
                        "restart_pod": _legacy_restart,
                        "rollout_undo": _legacy_rollback,
                    }[mode]

                    # 按轮次历史决定下一步，使同一脚本能在独立进程中从 checkpoint 继续。
                    class CardModel(ScriptedChatModel):
                        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                            frame = str(messages)
                            if "暂停点之前的会话" in frame and "本轮查询范围" in frame:
                                self.responses[:] = [AIMessage(content=SIDE)]
                            elif "request-action" in [
                                getattr(message, "tool_call_id", None) for message in messages
                            ]:
                                self.responses[:] = [
                                    report_call(
                                        conclusion=FINISHED, supporting=observation_ids(messages)
                                    )
                                ]
                            elif not self.seen:
                                business = (
                                    call(
                                        "prometheus_query_range",
                                        {
                                            "expression": REPLICA_PROBE,
                                            "lookback_seconds": 120,
                                            "step_seconds": 15,
                                        },
                                        "probe",
                                    )
                                    if mode != "restart_pod"
                                    else call(
                                        "kubernetes_list_logs",
                                        {"selector": "app=target", "pod_name": "target-pod"},
                                        "logs",
                                    )
                                )
                                self.responses[:] = [
                                    create_hypothesis(),
                                    call(
                                        "kubernetes_list_pods", {"selector": "app=target"}, "pods"
                                    ),
                                    business,
                                    _assess,
                                    request,
                                ]
                            return super()._generate(messages, stop, run_manager, **kwargs)

                    driver.model = CardModel(responses=[])
                else:
                    driver.model = _Model(
                        responses=[DEPLOYMENTS, AIMessage(content=f"{ANSWER} {PRIVATE}")]
                    )
                yield driver

        cli.open_conversation = open_test_conversation

    return cli.main()


if __name__ == "__main__":
    raise SystemExit(_child_main())
