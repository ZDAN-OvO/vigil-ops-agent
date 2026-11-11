"""验证对外 CLI 契约，避免骨架把未实现能力报告为成功。"""

import asyncio
import json
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


def test_help_does_not_require_external_services():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "status" in result.stdout


def test_status_json_reports_only_implemented_capabilities():
    result = runner.invoke(app, ["status", "--output", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["stage"] == "W0"
    assert payload["capabilities"]["investigation"] is True
    assert payload["capabilities"]["remediation"] is True
    assert payload["external_services_checked"] is False


def test_unimplemented_action_is_not_a_successful_noop():
    result = runner.invoke(app, ["act"])
    assert result.exit_code != 0


def test_followups_rejects_an_unknown_status():
    result = runner.invoke(app, ["followups", "--status", "bogus"])

    assert result.exit_code == 2
    assert "未知状态" in result.output


def test_followup_resolve_requires_a_note():
    result = runner.invoke(app, ["followup", "fol-x", "--resolve"])

    assert result.exit_code == 2
    assert "非空" in result.output


def test_followup_resolve_requires_an_actor():
    result = runner.invoke(
        app, ["followup", "fol-x", "--resolve", "--actor", "", "--note", "已核对"]
    )

    assert result.exit_code == 2
    assert "actor" in result.output


@pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL，跳过真实库测试")
def test_followup_reports_a_missing_id():
    runner.invoke(app, ["db", "migrate"])
    result = runner.invoke(app, ["followup", "fol-missing"])

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
        result = runner.invoke(app, ["followups"])

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
                "followup",
                followup.id,
                "--resolve",
                "--actor",
                "tester",
                "--note",
                "已核对",
            ],
        )
        assert result.exit_code == 0
        assert "resolved" in result.output

        after = runner.invoke(app, ["followups"])
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
