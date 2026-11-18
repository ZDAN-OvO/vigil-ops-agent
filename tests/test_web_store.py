"""会话事务、防重和工作领取的 PostgreSQL 验证。"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from aiops.config import Settings
from aiops.contracts import (
    ActionAttempt,
    ActionKind,
    ActionProposal,
    ActionTarget,
    AttemptStatus,
    Incident,
    InvestigationTask,
    ProposalStatus,
    RiskDecision,
    RiskDisposition,
    TaskPhase,
    TimeRange,
)
from aiops.db import Store
from aiops.webhook import build_investigation

DSN = Settings().database_url
pytestmark = pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL")


@pytest.fixture
def web_dsn():
    schema = f"web_test_{uuid4().hex}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield make_conninfo(DSN, options=f"-csearch_path={schema}")
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def new_investigation(symptoms="首页 500"):
    now = datetime.now(UTC)
    incident = Incident(
        id=f"inc-{uuid4().hex}",
        environment="test",
        cluster="vigil-lab",
        namespace="vigil-shop",
        service="frontend",
        trigger="manual",
        symptoms=symptoms,
        time_range=TimeRange(start=now - timedelta(minutes=10), end=now),
    )
    return incident, build_investigation(incident, Settings(model_name="test"), now=now)


def test_web_create_replay_and_conflicting_content(web_dsn):
    from aiops.db import ConversationConflict

    async def scenario():
        async with Store.open(web_dsn) as store:
            await store.migrate()
            first = await store.create_conversation("create", *new_investigation())
            repeat = await store.create_conversation("create", *new_investigation())
            assert repeat.id == first.id
            assert not first.replayed and repeat.replayed
            assert [e["kind"] for e in await store.list_conversation_events(first.id)] == [
                "message"
            ]
            with pytest.raises(ConversationConflict, match="idempotency_conflict"):
                await store.create_conversation("create", *new_investigation("更换内容"))
            with pytest.raises(ConversationConflict, match="conversation_active"):
                await store.create_conversation("other", *new_investigation())
            assert await store.get_current_conversation() is not None

    asyncio.run(scenario())


def test_web_created_once_under_concurrent_requests(web_dsn):
    from aiops.db import ConversationConflict

    async def scenario():
        async with Store.open(web_dsn) as store:
            await store.migrate()
            outcomes = await asyncio.gather(
                store.create_conversation("one", *new_investigation()),
                store.create_conversation("two", *new_investigation()),
                return_exceptions=True,
            )
            errors = [item for item in outcomes if isinstance(item, ConversationConflict)]
            assert len(errors) == 1 and errors[0].code == "conversation_active"
            assert sum(not isinstance(item, BaseException) for item in outcomes) == 1
            async with store._pool.connection() as conn:
                for table in (
                    "incidents",
                    "investigations",
                    "investigation_tasks",
                    "conversations",
                    "conversation_messages",
                    "conversation_events",
                    "web_requests",
                    "web_work",
                ):
                    cur = await conn.execute(
                        sql.SQL("SELECT count(*) AS total FROM {}").format(sql.Identifier(table))
                    )
                    assert (await cur.fetchone())["total"] == 1, table

    asyncio.run(scenario())


def test_web_append_replay_and_scope_conflicts(web_dsn):
    from aiops.db import ConversationConflict

    async def scenario():
        async with Store.open(web_dsn) as store:
            await store.migrate()
            created = await store.create_conversation("create", *new_investigation())
            first, repeat = await asyncio.gather(
                store.append_conversation_message("hint", created.id, "补充线索"),
                store.append_conversation_message("hint", created.id, "补充线索"),
            )
            assert first.id == repeat.id
            assert first.replayed != repeat.replayed
            events = await store.list_conversation_events(created.id)
            assert len(events) == 2
            assert await store.list_conversation_events(created.id, after=events[0]["id"]) == [
                events[1]
            ]
            for request_id, conv, text in [
                ("hint", created.id, "不同内容"),
                ("hint", "conv-other", "补充线索"),
                ("create", created.id, "补充线索"),
            ]:
                with pytest.raises(ConversationConflict, match="idempotency_conflict"):
                    await store.append_conversation_message(request_id, conv, text)
            with pytest.raises(ConversationConflict, match="not_found"):
                await store.append_conversation_message("missing", "conv-other", "补充线索")

    asyncio.run(scenario())


def test_web_claim_work_and_task_lease_are_atomic(web_dsn):
    async def scenario():
        async with Store.open(web_dsn) as store:
            await store.migrate()
            incident, investigation = new_investigation()
            created = await store.create_conversation("create", incident, investigation)
            results = await asyncio.gather(
                store.claim_web_work(created.id, "first"),
                store.claim_web_work(created.id, "second"),
            )
            assert sum(item is not None for item in results) == 1
            task = await store.get_task(investigation.id)
            assert task.lease_owner == ("first" if results[0] is not None else "second")
            assert task.lease_expires_at > datetime.now(UTC)
            assert await store.claim_web_work(created.id, "third") is None

    asyncio.run(scenario())


def test_web_migration_preserves_preexisting_investigation(web_dsn, monkeypatch):
    from aiops import db

    migration_paths = db.migration_files()

    async def scenario():
        incident, investigation = new_investigation()
        monkeypatch.setattr(db, "migration_files", lambda: migration_paths[:-1])
        async with Store.open(web_dsn) as store:
            assert len(await store.migrate()) == 12
            await store.save_incident(incident)
            await store.create_investigation(investigation)
            await store.create_task(
                InvestigationTask(
                    investigation_id=investigation.id,
                    phase=TaskPhase.RUNNING,
                    calendar_started_at=investigation.started_at,
                )
            )
            monkeypatch.setattr(db, "migration_files", lambda: migration_paths)
            assert await store.migrate() == ["0013_web_conversation"]
            assert await store.get_investigation(investigation.id) == investigation
            assert await store.get_current_conversation() is None
            task = await store.get_task(investigation.id)
            assert task.ready_proposal_id is None
            assert task.actions_disabled is False

    asyncio.run(scenario())


def test_finish_attempt_preserves_existing_send_state(web_dsn):
    now = datetime.now(UTC)
    proposal = ActionProposal(
        id="act-test",
        kind=ActionKind.ROLLOUT_UNDO,
        target=ActionTarget(
            cluster="vigil-lab",
            namespace="vigil-shop",
            kind="Deployment",
            name="frontend",
            uid="test",
            resource_version="1",
        ),
        parameters={},
        idempotency_key="test",
        reason="测试",
        created_at=now,
    )
    attempt = ActionAttempt(
        id="att-test",
        proposal_id=proposal.id,
        status=AttemptStatus.EXECUTING,
        idempotency_key=proposal.idempotency_key,
        snapshot={},
        request={},
        result={"send_state": "sent", "stage": "verifying"},
        started_at=now,
    )

    async def scenario():
        async with Store.open(web_dsn) as store:
            await store.migrate()
            await store.save_proposal(
                proposal,
                RiskDecision(
                    proposal_id=proposal.id,
                    disposition=RiskDisposition.APPROVAL,
                    policy_version="test",
                    reasons=["测试"],
                    requires_compensation=False,
                ),
            )
            await store.save_attempt(attempt)
            await store.finish_attempt(
                attempt.id,
                proposal_id=proposal.id,
                proposal_status=ProposalStatus.SUCCEEDED,
                status=AttemptStatus.VERIFIED,
                result={"stage": "done", "verification_done": True},
                verification=None,
                finished_at=datetime.now(UTC),
            )
            saved = await store.get_attempt(proposal.id)
            assert saved.result == {
                "send_state": "sent",
                "stage": "done",
                "verification_done": True,
            }

    asyncio.run(scenario())


async def seed_work(store, *, ready=False, proposal_status="pending_approval"):
    from actions_support import make_proposal

    incident, investigation = new_investigation()
    conv = await store.create_conversation("create", incident, investigation)
    work = await store.claim_web_work(conv.id, "old-instance")
    proposal = make_proposal(investigation_id=investigation.id, status=proposal_status)
    await store.save_proposal(
        proposal,
        RiskDecision(
            proposal_id=proposal.id,
            disposition=RiskDisposition.APPROVAL,
            policy_version="test",
            reasons=["测试"],
            requires_compensation=False,
        ),
    )
    if ready:
        from aiops.web.lifecycle import handoff

        await store.set_task_waiting(investigation.id, proposal_id=proposal.id)
        assert (await store.get_task(investigation.id)).ready_proposal_id is None
        await handoff(store, work, proposal.id)
    return conv.id, investigation, work, proposal


def test_web_decision_is_atomic_and_idempotent(web_dsn, monkeypatch):
    from aiops.contracts import ApprovalDecision
    from aiops.db import ConversationConflict
    from aiops.web.card import card_hash
    from aiops.web.lifecycle import decide_proposal

    async def scenario():
        async with Store.open(web_dsn) as store:
            await store.migrate()
            _, investigation, _, proposal = await seed_work(store, ready=True)
            async with store._pool.connection() as conn:
                fingerprint = await card_hash(conn, proposal)
            args = dict(
                request_id="decision",
                decision=ApprovalDecision.REJECTED,
                submitted_hash=fingerprint,
                reason="继续取证",
                actor="web-operator",
                ttl=900,
            )
            with pytest.raises(ConversationConflict, match="hash_mismatch"):
                await decide_proposal(store, proposal.id, **(args | {"submitted_hash": "wrong"}))
            real_insert = store._insert_approval

            async def fail(*args):
                raise RuntimeError("模拟决策途中失败")

            monkeypatch.setattr(store, "_insert_approval", fail)
            with pytest.raises(RuntimeError, match="模拟决策途中失败"):
                await decide_proposal(store, proposal.id, **args)
            current = await store.get_proposal(proposal.id)
            assert current.status is ProposalStatus.PENDING_APPROVAL
            monkeypatch.setattr(store, "_insert_approval", real_insert)
            first, second = await asyncio.gather(
                decide_proposal(store, proposal.id, **args),
                decide_proposal(store, proposal.id, **args),
            )
            assert first.id == second.id and first.replayed != second.replayed
            assert await store.count_approvals(proposal.id) == 1
            assert await store.get_attempt(proposal.id) is None
            assert (await store.get_task(investigation.id)).actions_disabled is True
            with pytest.raises(ConversationConflict, match="not_pending"):
                await decide_proposal(store, proposal.id, **(args | {"request_id": "other"}))

    asyncio.run(scenario())


async def seed_execution(store, proposal, send_state: str, *, verification_done=False):
    now = datetime.now(UTC)
    attempt = ActionAttempt(
        id="att-web-test",
        proposal_id=proposal.id,
        status=AttemptStatus.EXECUTING,
        idempotency_key=proposal.idempotency_key,
        snapshot={},
        request={},
        started_at=now,
        result={
            "send_state": send_state,
            "stage": "applying",
            "verification_done": verification_done,
        },
    )
    assert await store.acquire_lock(
        "vigil-lab/vigil-shop/Deployment/productcatalogservice", proposal.id
    )
    await store.save_attempt(attempt)
    return attempt


def test_recovery_row_1_skips_active_work(web_dsn):
    from aiops.web.lifecycle import recover_conversation

    async def scenario():
        async with Store.open(web_dsn) as store:
            await store.migrate()
            conv, _, _, proposal = await seed_work(store)
            await seed_execution(store, proposal, "sending")
            assert await recover_conversation(store, conv, "new", active=True) == "active"
            assert await recover_conversation(store, conv, "old-instance") == "active"
            assert (await store.get_attempt(proposal.id)).status is AttemptStatus.EXECUTING

    asyncio.run(scenario())


def test_recovery_row_2_terminal_task_closes_conversation(web_dsn):
    from aiops.web.lifecycle import recover_conversation

    async def scenario():
        async with Store.open(web_dsn) as store:
            await store.migrate()
            conv, investigation, _, _ = await seed_work(store)
            async with store._pool.connection() as conn:
                await conn.execute(
                    "UPDATE investigation_tasks SET phase = 'terminal', outcome = 'aborted', "
                    "termination_code = 'runtime_error' WHERE investigation_id = %s",
                    (investigation.id,),
                )
            assert await recover_conversation(store, conv, "new") == "terminal"
            assert await recover_conversation(store, conv, "new") == "finished"

    asyncio.run(scenario())


@pytest.mark.parametrize("sent", ["sending", "sent"])
def test_recovery_row_3_startup_recovers_unfinished_write(web_dsn, sent):
    from aiops.web.runner import ConversationRunner

    async def scenario():
        async with Store.open(web_dsn) as store:
            await store.migrate()
            _, _, _, proposal = await seed_work(store, proposal_status="executing")
            attempt = await seed_execution(store, proposal, sent)
            assert await store.has_unfinished_attempt()
            runner = ConversationRunner(store, Settings(database_url=web_dsn))
            await runner.startup()
            assert not await store.has_unfinished_attempt()
            current = await store.get_current_conversation()
            assert current["finished_at"] is not None
            assert (await store.get_followup_by_attempt(attempt.id)).status.value == "open"
            outcome = await store.get_attempt(proposal.id)
            assert outcome.result["send_state"] == ("sent" if sent == "sent" else "uncertain")
            assert outcome.status is AttemptStatus.UNKNOWN
            second = ConversationRunner(store, Settings(database_url=web_dsn))
            await second.startup()
            assert len(await store.list_followups(proposal_id=proposal.id)) == 1

    asyncio.run(scenario())


def test_recovery_row_4_final_write_closes_read_only(web_dsn):
    from aiops.web.lifecycle import recover_conversation

    async def scenario():
        async with Store.open(web_dsn) as store:
            await store.migrate()
            conv, _, _, proposal = await seed_work(store, proposal_status="succeeded")
            attempt = await seed_execution(store, proposal, "sent", verification_done=True)
            await store.finish_attempt(
                attempt.id,
                proposal_id=proposal.id,
                proposal_status=ProposalStatus.SUCCEEDED,
                status=AttemptStatus.VERIFIED,
                result=None,
                verification={"outcome": "improved"},
                finished_at=datetime.now(UTC),
                target_lock_key="vigil-lab/vigil-shop/Deployment/productcatalogservice",
            )
            assert await recover_conversation(store, conv, "new") == "closed_after_action"
            assert await store.get_followup_by_attempt(attempt.id) is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("result", "expected", "absent"),
    [
        (
            {"send_state": "sent", "verification_done": True, "verification": "improved"},
            ("已写入", "改善"),
            "未执行",
        ),
        (
            {"send_state": "not_sent", "not_executed_reason": "expired"},
            ("未执行", "授权已过期"),
            "已写入",
        ),
    ],
)
def test_recovery_row_4_conclusion_states_recorded_outcome(web_dsn, result, expected, absent):
    """只读收尾的结论必须写出实际结果：已写入且改善与未执行（过期）不能是同一句话。"""
    from aiops.web.lifecycle import recover_conversation

    async def scenario():
        async with Store.open(web_dsn) as store:
            await store.migrate()
            conv, _, _, proposal = await seed_work(store, proposal_status="succeeded")
            attempt = await seed_execution(store, proposal, result["send_state"])
            await store.finish_attempt(
                attempt.id,
                proposal_id=proposal.id,
                proposal_status=ProposalStatus.SUCCEEDED,
                status=AttemptStatus.VERIFIED,
                result=result,
                verification={"outcome": result.get("verification")},
                finished_at=datetime.now(UTC),
                target_lock_key="vigil-lab/vigil-shop/Deployment/productcatalogservice",
            )
            assert await recover_conversation(store, conv, "new") == "closed_after_action"
            async with store._pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT content FROM conversation_messages "
                    "WHERE conversation_id = %s AND kind = 'conclusion'",
                    (conv,),
                )
                return (await cur.fetchone())["content"]

    text = asyncio.run(scenario())
    assert all(word in text for word in expected), text
    assert absent not in text, text


def test_recovery_row_5_queued_work_starts(web_dsn):
    from aiops.web.lifecycle import recover_conversation

    async def scenario():
        async with Store.open(web_dsn) as store:
            await store.migrate()
            conv = await store.create_conversation("create", *new_investigation())
            assert await recover_conversation(store, conv.id, "new") == "queued"
            assert await store.claim_web_work(conv.id, "new") is not None

    asyncio.run(scenario())


def test_recovery_row_6_preserves_ready_proposal(web_dsn):
    from aiops.web.lifecycle import recover_conversation

    async def scenario():
        async with Store.open(web_dsn) as store:
            await store.migrate()
            conv, investigation, _, proposal = await seed_work(store, ready=True)
            assert await recover_conversation(store, conv, "new") == "waiting"
            assert (await store.get_task(investigation.id)).ready_proposal_id == proposal.id
            assert await store.claim_web_work(conv, "new") is None

    asyncio.run(scenario())


def test_recovery_row_7_interrupts_claimed_work_and_releases_target(web_dsn):
    from aiops.web.lifecycle import recover_conversation

    async def scenario():
        async with Store.open(web_dsn) as store:
            await store.migrate()
            conv, _, _, proposal = await seed_work(store, proposal_status="approved")
            key = "vigil-lab/vigil-shop/Deployment/productcatalogservice"
            assert await store.acquire_lock(key, proposal.id)
            assert await recover_conversation(store, conv, "new") == "interrupted"
            assert not await store.target_is_locked(key)
            assert (await store.get_current_conversation())["interrupted"]
            assert (await store.get_proposal(proposal.id)).status is ProposalStatus.REJECTED

    asyncio.run(scenario())


def test_web_handoff_rolls_back_ready_if_work_update_fails(web_dsn):
    from aiops.web.lifecycle import handoff

    async def scenario():
        async with Store.open(web_dsn) as store:
            await store.migrate()
            conv, investigation, work, proposal = await seed_work(store)
            await store.set_task_waiting(investigation.id, proposal_id=proposal.id)
            with pytest.raises(RuntimeError, match="审批工作已失效"):
                await handoff(store, work | {"id": work["id"] + 1}, proposal.id)
            task = await store.get_task(investigation.id)
            assert task.ready_proposal_id is None and task.lease_owner == "old-instance"
            assert await store.claim_web_work(conv, "new") is None
            await handoff(store, work, proposal.id)
            task = await store.get_task(investigation.id)
            assert task.ready_proposal_id == proposal.id and task.lease_owner is None

    asyncio.run(scenario())


def test_web_renew_lease_preserves_current_owner_only(web_dsn):
    async def scenario():
        async with Store.open(web_dsn) as store:
            await store.migrate()
            _, investigation, _, _ = await seed_work(store)
            before = await store.get_task(investigation.id)
            assert not await store.renew_web_lease(investigation.id, "other")
            assert await store.renew_web_lease(investigation.id, "old-instance")
            assert (
                await store.get_task(investigation.id)
            ).lease_expires_at > before.lease_expires_at

    asyncio.run(scenario())


def test_recovery_and_followup_resolution_never_restore_unknown(web_dsn):
    from aiops.contracts import ActionFollowup, AttemptOutcome
    from aiops.web.lifecycle import recover_conversation

    async def scenario():
        async with Store.open(web_dsn) as store:
            await store.migrate()
            conv, _, _, proposal = await seed_work(store, proposal_status="executing")
            attempt = await seed_execution(store, proposal, "uncertain")
            await store.finish_attempt(
                attempt.id,
                proposal_id=proposal.id,
                proposal_status=ProposalStatus.UNKNOWN,
                status=AttemptStatus.UNKNOWN,
                result=None,
                verification=None,
                finished_at=datetime.now(UTC),
                followup=ActionFollowup(
                    id="fol-web-test",
                    proposal_id=proposal.id,
                    attempt_id=attempt.id,
                    cluster=proposal.target.cluster,
                    namespace=proposal.target.namespace,
                    target_kind=proposal.target.kind,
                    target_name=proposal.target.name,
                    outcome=AttemptOutcome.UNKNOWN,
                    reason="待核实",
                    evidence={},
                    created_at=datetime.now(UTC),
                ),
            )
            await asyncio.gather(
                recover_conversation(store, conv, "new"),
                store.resolve_followup("fol-web-test", actor="tester", note="核实后无重复写入"),
            )
            assert (await store.get_proposal(proposal.id)).status is ProposalStatus.FAILED
            assert (await store.get_followup("fol-web-test")).status.value == "resolved"
            assert await recover_conversation(store, conv, "new") == "finished"
            assert (await store.get_attempt(proposal.id)).result["send_state"] == "uncertain"
            assert not await store.target_is_locked(
                "vigil-lab/vigil-shop/Deployment/productcatalogservice"
            )
            assert len(await store.list_followups(proposal_id=proposal.id)) == 1

    asyncio.run(scenario())


def test_web_approval_rejects_unknown_owner_and_unready_proposal(web_dsn):
    from aiops.contracts import ApprovalDecision
    from aiops.db import ConversationConflict
    from aiops.web.lifecycle import decide_proposal

    async def scenario():
        async with Store.open(web_dsn) as store:
            await store.migrate()
            _, _, _, proposal = await seed_work(store)
            for proposal_id, code in [(proposal.id, "not_waiting"), ("act-wrong", "not_found")]:
                with pytest.raises(ConversationConflict, match=code):
                    await decide_proposal(
                        store,
                        proposal_id,
                        request_id="test",
                        decision=ApprovalDecision.APPROVED,
                        submitted_hash="unused",
                        reason="",
                        actor="web-operator",
                        ttl=900,
                    )
            assert await store.count_approvals(proposal.id) == 0
            assert await store.get_attempt(proposal.id) is None

    asyncio.run(scenario())


def test_web_hints_checkpoint_and_model_retry_keep_one_logical_round(web_dsn):
    from investigation_support import FakeToolbox, ScriptedChatModel, report_call

    from aiops.agent.engine import LangChainInvestigation
    from aiops.web.runner import ConversationRunner

    async def scenario():
        settings = Settings(database_url=web_dsn, model_name="scripted")
        async with Store.open(web_dsn) as store:
            await store.migrate()
            incident, investigation = new_investigation()
            conv = await store.create_conversation("create", incident, investigation)
            hint = await store.append_conversation_message("hint", conv.id, "先检查首页依赖")
            worker = ConversationRunner(store, settings)
            await store.claim_web_work(conv.id, worker.instance_id)

            def transient_error(messages):
                raise TimeoutError("模拟第一次模型调用超时")

            model = ScriptedChatModel(responses=[transient_error, report_call(confirmed=False)])
            runtime = LangChainInvestigation(
                settings=settings,
                store=store,
                dsn=web_dsn,
                incident=incident,
                investigation=investigation,
                toolbox=FakeToolbox(),
                executor=None,
                model=model,
                arm="generic",
                conversation_id=conv.id,
                serve_instance=worker.instance_id,
            )
            await runtime.run()
            async with store._pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT delivery, delivered_round FROM conversation_messages WHERE id = %s",
                    (hint.id,),
                )
                message = await cur.fetchone()
                assert message == {"delivery": "delivered", "delivered_round": 1}
            assert len(model.seen) >= 2
            for messages in model.seen:
                assert sum("先检查首页依赖" in str(m.content) for m in messages) == 1

    asyncio.run(scenario())


def test_web_cli_cannot_mutate_page_investigation_after_serve_stops(web_dsn, monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from aiops.cli import app

    async def scenario():
        async with Store.open(web_dsn) as store:
            await store.migrate()
            _, investigation, _, proposal = await seed_work(store, ready=True)
            return proposal.id, investigation.id

    proposal_id, investigation_id = asyncio.run(scenario())
    monkeypatch.setenv("VIGIL_DATABASE_URL", web_dsn)
    monkeypatch.setenv("VIGIL_RUNTIME_DIR", str(tmp_path))
    for args in (
        ["approve", proposal_id],
        ["reject", proposal_id],
        ["resume", proposal_id],
        ["cancel", investigation_id],
        [
            "act",
            "--deployment",
            "frontend",
            "--reason",
            "test",
            "--investigation",
            investigation_id,
        ],
    ):
        result = CliRunner().invoke(app, ["dev", *args])
        assert result.exit_code == 1, args
        assert "页面" in result.output


def test_closed_followup_before_recovery_supplies_readonly_conclusion(web_dsn):
    from aiops.contracts import ActionFollowup, AttemptOutcome
    from aiops.web.lifecycle import recover_conversation

    async def scenario():
        async with Store.open(web_dsn) as store:
            await store.migrate()
            conv, _, _, proposal = await seed_work(store, proposal_status="executing")
            attempt = await seed_execution(store, proposal, "uncertain")
            followup = ActionFollowup(
                id="fol-closed-first",
                proposal_id=proposal.id,
                attempt_id=attempt.id,
                cluster=proposal.target.cluster,
                namespace=proposal.target.namespace,
                target_kind=proposal.target.kind,
                target_name=proposal.target.name,
                outcome=AttemptOutcome.UNKNOWN,
                reason="未知",
                evidence={},
                created_at=datetime.now(UTC),
            )
            await store.finish_attempt(
                attempt.id,
                proposal_id=proposal.id,
                proposal_status=ProposalStatus.UNKNOWN,
                status=AttemptStatus.UNKNOWN,
                result=None,
                verification=None,
                finished_at=datetime.now(UTC),
                followup=followup,
            )
            await store.resolve_followup(followup.id, actor="tester", note="现场已核实")
            assert await recover_conversation(store, conv, "new") == "closed_after_action"
            current = await store.get_current_conversation()
            assert current["finished_at"] and not current["interrupted"]
            assert (await store.get_proposal(proposal.id)).status is ProposalStatus.FAILED
            async with store._pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT content FROM conversation_messages "
                    "WHERE conversation_id = %s AND kind = 'conclusion'",
                    (conv,),
                )
                assert "现场已核实" in (await cur.fetchone())["content"]

    asyncio.run(scenario())


def test_recovery_prior_not_sent_does_not_hide_unready_new_proposal(web_dsn):
    from actions_support import make_proposal

    from aiops.web.lifecycle import recover_conversation

    async def scenario():
        async with Store.open(web_dsn) as store:
            await store.migrate()
            conv, inv, _, first = await seed_work(store, proposal_status="executing")
            attempt = await seed_execution(store, first, "not_sent")
            key = "vigil-lab/vigil-shop/Deployment/productcatalogservice"
            await store.finish_attempt(
                attempt.id,
                proposal_id=first.id,
                proposal_status=ProposalStatus.FAILED,
                status=AttemptStatus.FAILED,
                result={"stage": "done"},
                verification=None,
                finished_at=datetime.now(UTC),
                target_lock_key=key,
            )
            assert not await store.target_is_locked(key)
            second = make_proposal(
                id="act-second", investigation_id=inv.id, created_at=datetime.now(UTC)
            )
            policy = RiskDecision(
                proposal_id=second.id,
                disposition=RiskDisposition.APPROVAL,
                policy_version="test",
                reasons=["测试"],
                requires_compensation=False,
            )
            await store.save_proposal(second, policy)
            await store.set_task_waiting(inv.id, proposal_id=second.id)
            assert await recover_conversation(store, conv, "new") == "interrupted"
            assert (await store.get_proposal(second.id)).status is ProposalStatus.REJECTED
            third = second.model_copy(update={"id": "act-third"})
            await store.save_proposal(third, policy.model_copy(update={"proposal_id": third.id}))

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("read_only", "action", "disabled", "expected"),
    [
        (True, "rollout_undo", False, "告警调查只取证"),
        (True, "restart_pod", False, "告警调查只取证"),
        (False, "restart_pod", False, "本次调查不支持该动作"),
        (False, "rollout_undo", True, "已被人工拒绝处置"),
    ],
)
def test_web_request_action_scope(web_dsn, read_only, action, disabled, expected):
    from investigation_support import FakeToolbox, ScriptedChatModel

    from aiops.agent.engine import LangChainInvestigation
    from aiops.agent.schemas import RequestActionArgs
    from aiops.agent.tools import request_action_core

    async def scenario():
        async with Store.open(web_dsn) as store:
            await store.migrate()
            incident, investigation = new_investigation()
            conv = await store.create_conversation("create", incident, investigation)
            async with store._pool.connection() as conn:
                await conn.execute(
                    "UPDATE investigation_tasks SET actions_disabled = %s "
                    "WHERE investigation_id = %s",
                    (disabled, investigation.id),
                )
            worker = LangChainInvestigation(
                settings=Settings(database_url=web_dsn, model_name="test"),
                store=store,
                dsn=web_dsn,
                incident=incident,
                investigation=investigation,
                toolbox=FakeToolbox(),
                executor=None,
                model=ScriptedChatModel(responses=[]),
                read_only=read_only,
                conversation_id=None if read_only else conv.id,
            )
            _, context = await worker._open_context()
            await context.budget.start()
            result = await request_action_core(
                context,
                "test-scope",
                RequestActionArgs(
                    action=action,
                    target_deployment="productcatalogservice",
                    target_pod="test",
                    service="frontend",
                    reason="测试",
                    hypothesis_id="test",
                    evidence_ids=["test"],
                ),
            )
            assert expected in result
            assert await store.count_proposals_for_investigation(investigation.id) == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("state", ["sending", "sent"])
def test_serve_opens_http_only_after_stale_page_write_is_recovered(web_dsn, tmp_path, state):
    import os
    import socket

    import httpx
    from test_cli import cli_command, running_command, wait_for_file

    async def seed():
        async with Store.open(web_dsn) as store:
            await store.migrate()
            _, _, _, proposal = await seed_work(store, proposal_status="executing")
            await seed_execution(store, proposal, state)

    asyncio.run(seed())
    with socket.socket() as socket_probe:
        socket_probe.bind(("127.0.0.1", 0))
        port = socket_probe.getsockname()[1]
    env = {
        **os.environ,
        "VIGIL_DATABASE_URL": web_dsn,
        "VIGIL_RUNTIME_DIR": str(tmp_path),
        "VIGIL_WEB_HOST": "127.0.0.1",
        "VIGIL_WEB_PORT": str(port),
    }
    log = tmp_path / "serve.log"
    with running_command(
        cli_command("serve", "--host", "127.0.0.1", "--port", "0"), env, log
    ) as proc:
        base = f"http://127.0.0.1:{port}"
        wait_for_file(log, f"Uvicorn running on {base}", proc)
        response = httpx.get(f"{base}/api/conversations/current", timeout=5)
        assert response.status_code == 200
        result = response.json()
        assert result["conversation"]["finished"]
        assert result["conversation"]["page_state"] == "needs_verification"
        assert result["outcome"]["send_state"] == ("sent" if state == "sent" else "uncertain")
        assert result["outcome"]["verification_reason"] == "interrupted"
        assert len(result["followups"]) == 1


@pytest.mark.parametrize("missing", ["ownership", "card_hash", "human_approval"])
def test_serve_executor_refuses_unapproved_or_unowned_write(web_dsn, tmp_path, missing):
    from actions_support import FakeAdapter, FakePrometheus, make_proposal, rollout_plan

    from aiops.actions.executor import ActionExecutor, proposal_hash
    from aiops.actions.rollout_undo import ActionError
    from aiops.contracts import Approval, ApprovalDecision

    async def scenario():
        async with Store.open(web_dsn) as store:
            await store.migrate()
            if missing == "ownership":
                proposal = make_proposal(status=ProposalStatus.APPROVED)
                await store.save_proposal(
                    proposal,
                    RiskDecision(
                        proposal_id=proposal.id,
                        disposition=RiskDisposition.APPROVAL,
                        policy_version="test",
                        reasons=["测试"],
                        requires_compensation=False,
                    ),
                )
            else:
                _, _, _, proposal = await seed_work(store, proposal_status="approved")
            await store.save_approval(
                Approval(
                    id="apr-scope",
                    proposal_id=proposal.id,
                    decision=ApprovalDecision.APPROVED,
                    actor="policy:test" if missing == "human_approval" else "web-operator",
                    card_hash=None if missing == "card_hash" else "fixed",
                    reason="测试",
                    decided_at=datetime.now(UTC),
                    proposal_hash=proposal_hash(proposal),
                )
            )
            adapter = FakeAdapter(rollout_plan())
            executor = ActionExecutor(
                store=store,
                adapter=adapter,
                prometheus=FakePrometheus([]),
                runtime_path=tmp_path,
                cluster="vigil-lab",
                namespace="vigil-shop",
                serve_instance="old-instance",
            )
            with pytest.raises(ActionError) as caught:
                await executor.execute_authorized(proposal.id)
            assert caught.value.code == "web_approval_required"
            assert not adapter.calls
            assert await store.get_attempt(proposal.id) is None

    asyncio.run(scenario())


def test_web_rollout_autorule_requires_human_decision(web_dsn, tmp_path, monkeypatch):
    from actions_support import FakeAdapter, FakePrometheus, rollout_plan

    from aiops.actions import executor as actions

    def allow(proposal, **kwargs):
        return RiskDecision(
            proposal_id=proposal.id,
            disposition=RiskDisposition.AUTO,
            policy_version="test",
            reasons=["模拟自动规则"],
            requires_compensation=False,
        )

    monkeypatch.setattr(actions, "decide", allow)

    async def scenario():
        async with Store.open(web_dsn) as store:
            await store.migrate()
            incident, investigation = new_investigation()
            await store.create_conversation("create", incident, investigation)
            adapter = FakeAdapter(rollout_plan())
            executor = actions.ActionExecutor(
                store=store,
                adapter=adapter,
                prometheus=FakePrometheus([]),
                runtime_path=tmp_path,
                cluster="vigil-lab",
                namespace="vigil-shop",
                serve_instance="test-instance",
            )
            proposal, decision = await executor.propose_rollback(
                deployment="productcatalogservice",
                reason="模拟回滚",
                actor="model-agent",
                investigation_id=investigation.id,
            )
            assert decision.disposition is RiskDisposition.APPROVAL
            assert proposal.status is ProposalStatus.PENDING_APPROVAL
            assert await store.get_approval(proposal.id) is None
            assert not adapter.calls

    asyncio.run(scenario())


def test_web_failed_sent_update_is_recovered_at_terminal_close(web_dsn):
    from aiops.contracts import TaskOutcome
    from aiops.web.snapshot import read_snapshot

    async def scenario():
        settings = Settings(database_url=web_dsn)
        async with Store.open(web_dsn) as store:
            await store.migrate()
            _, inv, _, proposal = await seed_work(store, proposal_status="executing")
            attempt = await seed_execution(store, proposal, "sending")
            # 模拟集群已成功返回，但保存 sent 的服务连接失败。
            await store.finish_task(
                inv.id,
                outcome=TaskOutcome.ABORTED,
                termination_code="store_failure",
                finished_at=datetime.now(UTC),
            )
            snapshot = await read_snapshot(store, settings)
            assert snapshot.conversation.finished
            assert snapshot.conversation.page_state == "needs_verification"
            assert snapshot.outcome.send_state == "uncertain"
            assert (await store.get_followup_by_attempt(attempt.id)).status.value == "open"

    asyncio.run(scenario())


def test_web_checkpoint_recovery_confirms_hint_once(web_dsn):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.graph import END, START, StateGraph

    from aiops.web.hints import HintState
    from aiops.web.runner import ConversationRunner

    async def scenario():
        settings = Settings(database_url=web_dsn)
        async with Store.open(web_dsn) as store:
            await store.migrate()
            incident, inv = new_investigation()
            conv = await store.create_conversation("create", incident, inv)
            hint = await store.append_conversation_message("hint", conv.id, "已查到的信息")
            runner = ConversationRunner(store, settings)
            await store.claim_web_work(conv.id, "crashed-instance")
            async with AsyncPostgresSaver.from_conn_string(web_dsn) as saver:
                await saver.setup()
                graph = StateGraph(HintState)
                graph.add_node(
                    "reply",
                    lambda _: {
                        "logical_round": 1,
                        "successful_round": 1,
                        "hint_rounds": {hint.id: 1},
                    },
                )
                graph.add_edge(START, "reply")
                graph.add_edge("reply", END)
                await graph.compile(checkpointer=saver).ainvoke(
                    {"messages": []},
                    {"configurable": {"thread_id": inv.id}},
                    durability="sync",
                )
            assert len(await store.pending_conversation_hints(conv.id)) == 1
            await runner.startup()
            await runner.recover_current()
            events = await store.list_conversation_events(conv.id)
            assert sum(event["kind"] == "hint_delivered" for event in events) == 1
            async with store._pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT delivery, delivered_round FROM conversation_messages WHERE id = %s",
                    (hint.id,),
                )
                assert (await cur.fetchone()) == {"delivery": "delivered", "delivered_round": 1}

    asyncio.run(scenario())


def test_cli_pending_write_without_followup_can_be_adopted_and_resolved(web_dsn):
    """命令行登记 sending 后崩溃且无待办：拒绝启动 → 转待办 → 人工核实 → 启动成功。"""
    from actions_support import make_proposal

    from aiops.db import StoreUnavailable
    from aiops.web.runner import ConversationRunner

    async def scenario():
        async with Store.open(web_dsn) as store:
            await store.migrate()
            incident, investigation = new_investigation()
            await store.save_incident(incident)
            await store.create_investigation(investigation)
            proposal = make_proposal(investigation_id=investigation.id, status="executing")
            await store.save_proposal(
                proposal,
                RiskDecision(
                    proposal_id=proposal.id,
                    disposition=RiskDisposition.APPROVAL,
                    policy_version="test",
                    reasons=["测试"],
                    requires_compensation=False,
                ),
            )
            await seed_execution(store, proposal, "sending")
            settings = Settings(database_url=web_dsn)
            with pytest.raises(StoreUnavailable, match="adopt-pending"):
                await ConversationRunner(store, settings).startup()

            created = await store.adopt_unfinished_attempts()
            assert len(created) == 1
            assert await store.adopt_unfinished_attempts() == []
            kept = await store.get_attempt(proposal.id)
            assert kept.status is AttemptStatus.UNKNOWN
            assert kept.result["send_state"] == "uncertain"
            assert kept.result["stage"] == "applying"  # 原始写入证据保留
            assert await store.target_is_locked(
                "vigil-lab/vigil-shop/Deployment/productcatalogservice"
            )

            assert await store.resolve_followup(created[0], actor="op", note="集群已核实")
            assert not await store.target_is_locked(
                "vigil-lab/vigil-shop/Deployment/productcatalogservice"
            )
            await ConversationRunner(store, settings).startup()

    asyncio.run(scenario())
