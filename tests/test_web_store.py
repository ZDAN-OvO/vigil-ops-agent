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
