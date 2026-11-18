"""验证对外 CLI 契约，避免骨架把未实现能力报告为成功。"""

import asyncio
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from typer.testing import CliRunner

from aiops.cli import act_exit_code, app
from aiops.config import Settings
from aiops.contracts import (
    ActionAttempt,
    ActionFollowup,
    ActionKind,
    ActionProposal,
    ActionTarget,
    AttemptOutcome,
    AttemptStatus,
    ProposalStatus,
    RiskDecision,
    RiskDisposition,
)
from aiops.db import Store

runner = CliRunner()
DSN = Settings().database_url
DB_REQUIRED = pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL")


def test_help_does_not_require_external_services():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    from typer.main import get_command

    cli = get_command(app)
    assert {name for name, command in cli.commands.items() if not command.hidden} == {
        "doctor",
        "serve",
    }
    assert "serve" in result.stdout
    assert "doctor" in result.stdout
    assert "investigate" not in result.stdout
    assert "dev" not in result.stdout


def test_lab_scenarios_lists_truth_and_expected_action_without_external_services():
    result = runner.invoke(app, ["dev", "lab", "scenarios", "--output", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    items = {item["id"]: item for item in payload["scenarios"]}
    assert set(items) == {
        "f01-bad-release",
        "f02-memory-limit-oom",
        "f03-redis-latency",
        "a01-pod-rebuild",
    }
    assert items["f01-bad-release"]["expected_action"] == "rollout_undo"
    assert items["f02-memory-limit-oom"]["expected_action"] == "none"
    assert items["a01-pod-rebuild"]["expected_action"] == "restart_pod"
    for item in items.values():
        assert item["truth"]
        assert item["evidence_checks"]


def test_lab_summarize_reports_a_missing_record():
    result = runner.invoke(app, ["dev", "lab", "summarize", "--record", "/tmp/does-not-exist.json"])
    assert result.exit_code == 2


def test_status_json_reports_only_implemented_capabilities():
    result = runner.invoke(app, ["dev", "status", "--output", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["stage"] == "W0"
    assert payload["capabilities"]["investigation"] is True
    assert payload["capabilities"]["remediation"] is True
    assert payload["external_services_checked"] is False


def test_unimplemented_action_is_not_a_successful_noop():
    result = runner.invoke(app, ["dev", "act"])
    assert result.exit_code != 0


def test_followups_rejects_an_unknown_status():
    result = runner.invoke(app, ["dev", "followups", "--status", "bogus"])

    assert result.exit_code == 2
    assert "未知状态" in result.output


def test_followup_resolve_requires_a_note():
    result = runner.invoke(app, ["dev", "followup", "fol-x", "--resolve"])

    assert result.exit_code == 2
    assert "非空" in result.output


def test_followup_resolve_requires_an_actor():
    result = runner.invoke(
        app, ["dev", "followup", "fol-x", "--resolve", "--actor", "", "--note", "已核对"]
    )

    assert result.exit_code == 2
    assert "actor" in result.output


@pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL，跳过真实库测试")
def test_followup_reports_a_missing_id():
    runner.invoke(app, ["dev", "db", "migrate"])
    result = runner.invoke(app, ["dev", "followup", "fol-missing"])

    assert result.exit_code == 1
    assert "不存在" in result.output


def seed_followup():
    suffix = uuid4().hex[:8]
    proposal = ActionProposal(
        id=f"act-cli-{suffix}",
        kind=ActionKind.RESTART_POD,
        target=ActionTarget(
            cluster="vigil-lab",
            namespace="vigil-experiment",
            kind="Pod",
            name="pod-1",
            uid="uid-1",
            resource_version="1",
        ),
        parameters={
            "workload": "w1",
            "owner_kind": "ReplicaSet",
            "discardable": True,
            "service": "svc",
        },
        idempotency_key=f"cli-{suffix}",
        reason="CLI 测试",
        created_at=datetime.now(UTC),
    )
    attempt = ActionAttempt(
        id=f"att-cli-{suffix}",
        proposal_id=proposal.id,
        status=AttemptStatus.EXECUTING,
        idempotency_key=proposal.idempotency_key,
        snapshot={},
        request={},
        started_at=datetime.now(UTC),
    )
    followup = ActionFollowup(
        id=f"fol-cli-{suffix}",
        proposal_id=proposal.id,
        attempt_id=attempt.id,
        cluster="vigil-lab",
        namespace="vigil-experiment",
        target_kind="Pod",
        target_name="pod-1",
        outcome=AttemptOutcome.WORSENED,
        reason="CLI 测试",
        evidence={},
        created_at=datetime.now(UTC),
    )

    async def seed():
        async with Store.open(DSN) as store:
            await store.migrate()
            decision = RiskDecision(
                proposal_id=proposal.id,
                disposition=RiskDisposition.APPROVAL,
                policy_version="test",
                reasons=["测试"],
                requires_compensation=False,
            )
            await store.save_proposal(proposal, decision)
            await store.save_attempt(attempt)
            await store.finish_attempt(
                attempt.id,
                proposal_id=proposal.id,
                proposal_status=ProposalStatus.FAILED,
                status=AttemptStatus.FAILED,
                result={},
                verification={"outcome": "worsened"},
                finished_at=datetime.now(UTC),
                followup=followup,
            )

    asyncio.run(seed())
    return proposal, attempt, followup


def cleanup_followup(proposal_id):
    with psycopg.connect(DSN) as conn:
        conn.execute("DELETE FROM action_followups WHERE proposal_id = %s", (proposal_id,))
        conn.execute("DELETE FROM audit_events WHERE correlation_id = %s", (proposal_id,))
        conn.execute("DELETE FROM action_attempts WHERE proposal_id = %s", (proposal_id,))
        conn.execute("DELETE FROM action_proposals WHERE id = %s", (proposal_id,))


@pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL，跳过真实库测试")
def test_followups_lists_proposal_and_attempt_ids():
    proposal, attempt, followup = seed_followup()
    try:
        result = runner.invoke(app, ["dev", "followups"])

        assert result.exit_code == 0
        assert followup.id in result.output
        assert proposal.id in result.output
        assert attempt.id in result.output
        assert "提案" in result.output
        assert "执行" in result.output
    finally:
        cleanup_followup(proposal.id)


@pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL，跳过真实库测试")
def test_followup_resolve_success():
    proposal, attempt, followup = seed_followup()
    try:
        result = runner.invoke(
            app,
            [
                "dev",
                "followup",
                "resolve",
                followup.id,
                "--actor",
                "tester",
                "--note",
                "已核对",
            ],
        )
        assert result.exit_code == 0
        assert "resolved" in result.output

        after = runner.invoke(app, ["dev", "followups"])
        assert followup.id not in after.output
    finally:
        cleanup_followup(proposal.id)


def test_act_exit_codes_follow_the_contract():
    assert act_exit_code("denied", None) == 4
    assert act_exit_code("pending_approval", None) == 3
    assert act_exit_code("succeeded", "verified") == 0
    assert act_exit_code("failed", "failed") == 1
    assert act_exit_code("unknown", "unknown") == 1


def test_open_executor_context_manager_yields_the_executor(monkeypatch):
    from contextlib import asynccontextmanager

    from aiops import cli

    sentinel = object()

    @asynccontextmanager
    async def fake_open_executor(settings, *, store):
        yield sentinel

    monkeypatch.setattr(cli, "open_executor", fake_open_executor)

    async def run():
        async with cli._open_executor(Settings(), object()) as executor:
            return executor

    assert asyncio.run(run()) is sentinel


@pytest.fixture
def cli_env(tmp_path):
    return {
        **os.environ,
        "VIGIL_RUNTIME_DIR": str(tmp_path / "runtime"),
        "VIGIL_WEB_PORT": "0",
        "VIGIL_WEBHOOK_TOKEN": "test-webhook-token",
        "VIGIL_MODEL_NAME": "test-model",
    }


def cli_command(*args):
    return [sys.executable, "-m", "aiops.cli", *args]


def run_cli(env, *args):
    return subprocess.run(cli_command(*args), env=env, capture_output=True, text=True, timeout=20)


@contextmanager
def running_command(command, env, log_file):
    with log_file.open("w") as log:
        with subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT) as process:
            try:
                yield process
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=6)
                    except subprocess.TimeoutExpired:
                        process.kill()


def wait_for_file(path, substring, process):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if path.exists() and substring in path.read_text():
            return
        assert process.poll() is None, path.read_text() if path.exists() else process.returncode
        time.sleep(0.025)
    pytest.fail(f"进程未就绪：{path.read_text() if path.exists() else path}")


@DB_REQUIRED
def test_serve_holds_lock_against_dev_approve(tmp_path, cli_env):
    log = tmp_path / "serve.log"
    with running_command(cli_command("serve", "--port", "0"), cli_env, log) as process:
        wait_for_file(log, "Uvicorn running on", process)
        result = run_cli(cli_env, "dev", "approve", "act-not-present")
        assert result.returncode == 1
        assert "Vigil 正在运行，请在页面上操作" in result.stderr
        followups = run_cli(cli_env, "dev", "followups")
        assert followups.returncode == 0


@DB_REQUIRED
def test_serve_refuses_dev_writer_after_its_database_connection_closed(tmp_path, cli_env):
    log = tmp_path / "writer.log"
    code = """
import asyncio
from aiops.cli import app
from aiops.db import Store

original_migrate = Store.migrate

async def pause_after_db_disconnect(self):
    applied = await original_migrate(self)
    await self._pool.close()
    print("已断开数据库", flush=True)
    await asyncio.sleep(30)
    return applied

Store.migrate = pause_after_db_disconnect
app()
"""
    with running_command(
        [sys.executable, "-c", code, "dev", "db", "migrate"], cli_env, log
    ) as process:
        wait_for_file(log, "已断开数据库", process)
        result = run_cli(cli_env, "serve", "--port", "0")
        assert result.returncode == 1
        assert "持有推进者锁" in result.stderr
        assert process.poll() is None

    acquired = run_cli(cli_env, "dev", "db", "migrate")
    assert acquired.returncode == 0


@DB_REQUIRED
def test_serve_refuses_live_lab_script_even_after_parent_exits(tmp_path, cli_env):
    script = tmp_path / "scenario.sh"
    script_pid = tmp_path / "script.pid"
    script.write_text(f'printf "%s\n" "$$" > {shlex.quote(str(script_pid))}\nexec sleep 30\n')
    record = tmp_path / "record.json"
    log = tmp_path / "lab.log"
    code = """
import sys
from dataclasses import replace
from pathlib import Path
from aiops import cli

script, *args = sys.argv[1:]
sys.argv[1:] = args
scenario = replace(cli.SCENARIOS[0], script=Path(script))
cli.check_kube_context = lambda settings: "test"
cli.get_scenario = lambda name: scenario
cli.app()
"""
    command = [
        sys.executable,
        "-c",
        code,
        str(script),
        "dev",
        "lab",
        "run",
        "--scenario",
        "test",
        "--arm",
        "rules",
        "--runs",
        "1",
        "--record",
        str(record),
    ]
    with running_command(command, cli_env, log) as parent:
        wait_for_file(script_pid, "\n", parent)
        pid = int(script_pid.read_text())
        try:
            parent.kill()
            assert parent.wait(timeout=6) == -signal.SIGKILL
            os.kill(pid, 0)
            result = run_cli(cli_env, "serve", "--port", "0")
            assert result.returncode == 1
            assert "持有推进者锁" in result.stderr
        finally:
            os.kill(pid, signal.SIGTERM)

    acquired = run_cli(cli_env, "dev", "db", "migrate")
    assert acquired.returncode == 0


@DB_REQUIRED
@pytest.mark.parametrize("status", ["intent", "executing"])
def test_serve_refuses_unfinished_unowned_attempt_before_listening(status, cli_env):
    proposal, attempt, _ = seed_followup()
    try:
        with psycopg.connect(DSN) as conn:
            conn.execute(
                "UPDATE action_attempts SET status = %s WHERE id = %s", (status, attempt.id)
            )
        result = run_cli(cli_env, "serve", "--port", "0")
        assert result.returncode == 1
        assert "未决执行" in result.stderr
        assert "Uvicorn running on" not in result.stderr
    finally:
        cleanup_followup(proposal.id)
