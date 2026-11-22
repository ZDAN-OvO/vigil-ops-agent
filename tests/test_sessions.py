"""会话存储在真实 PostgreSQL 上的迁移、提交和多进程竞争回归。"""

import asyncio
import json
import os
import sys
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from typing import TypedDict
from uuid import uuid4

import psycopg
import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from psycopg import sql
from psycopg.conninfo import make_conninfo

from aiops import cli, db
from aiops.config import Settings
from aiops.contracts import (
    ActionKind,
    ActionOrigin,
    ActionProposal,
    ActionTarget,
    Budget,
    Hypothesis,
    Incident,
    Investigation,
    InvestigationStatus,
    InvestigationTask,
    ProposalStatus,
    RiskDecision,
    RiskDisposition,
    TaskPhase,
    TimeRange,
    ToolRequest,
    ToolRequestState,
)
from aiops.db import Store
from aiops.sessions.checkpoint import (
    INPUT_IDS_CHANNEL,
    TURN_ID_METADATA,
    SessionCheckpointer,
    setup_checkpoints,
)
from aiops.sessions.models import LeaseLost, SessionClosed
from aiops.sessions.store import SessionStore

DSN = os.environ.get("VIGIL_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL，跳过真实库测试")


@pytest.fixture
def session_dsn():
    schema = f"session_test_{uuid4().hex}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield make_conninfo(DSN, options=f"-csearch_path={schema}")
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _investigation():
    now = datetime.now(UTC)
    incident = Incident(
        id="inc-test",
        environment="test",
        cluster="test",
        namespace="test",
        service="test",
        trigger="manual",
        symptoms="测试",
        time_range=TimeRange(start=now - timedelta(minutes=1), end=now),
    )
    investigation = Investigation(
        id="inv-test",
        incident_id=incident.id,
        status=InvestigationStatus.RUNNING,
        model="test-model",
        policy_version="test-policy",
        budget=Budget(max_tool_calls=5, max_model_calls=5, max_wall_clock_seconds=60),
        started_at=now,
    )
    return incident, investigation


async def _running(store):
    sessions = SessionStore(store)
    session = await sessions.create_session(title="查询")
    first = await sessions.enqueue_input(session.id, "原文", display_text="显示文本")
    lease = await sessions.acquire_main_lease(session.id, "worker-1")
    turn = await sessions.start_main_turn(lease)
    return sessions, session, first, lease, turn


def _checkpoint(saver, turn, ids, *, parent=None):
    snapshot = empty_checkpoint()
    snapshot["channel_values"] = {INPUT_IDS_CHANNEL: ids}
    new_versions = {INPUT_IDS_CHANNEL: saver.get_next_version(None, None)}
    snapshot["channel_versions"] = new_versions
    config = parent or {
        "configurable": {"thread_id": turn.checkpoint_thread_id, "checkpoint_ns": ""}
    }
    return config, snapshot, {"source": "input", "step": -1}, new_versions


def test_upgrade_preserves_legacy_lease_checkpoint_and_action(session_dsn, monkeypatch, capsys):
    migrations = db.migration_files()
    baseline = [path for path in migrations if path.stem < "0012_sessions"]
    through_sessions = [path for path in migrations if path.stem <= "0012_sessions"]
    through_alert_inputs = [path for path in migrations if path.stem <= "0014_alert_input_names"]

    async def scenario():
        async with Store.open(session_dsn) as store, Store.open(session_dsn) as other:
            monkeypatch.setattr(db, "migration_files", lambda: baseline)
            assert len(await store.migrate()) == len(baseline)
            incident, investigation = _investigation()
            # 使用升级前的表布局写入旧业务数据。
            async with store.transaction() as conn:
                await conn.execute(
                    """
                    INSERT INTO incidents (
                        id, environment, cluster, namespace, service, trigger, symptoms,
                        time_start, time_end
                    ) VALUES (%s, 'test', 'test', 'test', 'test', 'manual', '测试', %s, %s)
                    """,
                    (incident.id, incident.time_range.start, incident.time_range.end),
                )
                await conn.execute(
                    """
                    INSERT INTO investigations (
                        id, incident_id, status, model, policy_version,
                        budget_tool_calls, budget_model_calls, budget_wall_clock_seconds,
                        started_at
                    ) VALUES (%s, %s, 'running', 'test', 'test', 5, 5, 60, %s)
                    """,
                    (investigation.id, incident.id, investigation.started_at),
                )
                await conn.execute(
                    """
                    INSERT INTO investigation_tasks (
                        investigation_id, phase, runtime_version, calendar_started_at,
                        lease_owner, lease_expires_at
                    ) VALUES (%s, 'running', 'legacy', clock_timestamp(),
                              'legacy-owner', clock_timestamp() + interval '60 seconds')
                    """,
                    (investigation.id,),
                )
            proposal = ActionProposal(
                id="legacy-proposal",
                investigation_id=investigation.id,
                origin=ActionOrigin.MODEL,
                kind=ActionKind.RESTART_POD,
                target=ActionTarget(
                    cluster="test",
                    namespace="test",
                    kind="Pod",
                    name="test",
                    uid="uid",
                    resource_version="1",
                ),
                parameters={},
                evidence_ids=["legacy-evidence"],
                idempotency_key="legacy",
                reason="历史提案",
                status=ProposalStatus.APPROVED,
                created_at=investigation.started_at,
            )
            await store.save_proposal(
                proposal,
                RiskDecision(
                    proposal_id=proposal.id,
                    disposition=RiskDisposition.APPROVAL,
                    reasons=["历史策略"],
                    requires_compensation=False,
                    policy_version="test",
                ),
            )
            assert await store.acquire_lock("legacy-key", proposal.id)

            async with AsyncPostgresSaver.from_conn_string(session_dsn) as legacy:
                await legacy.setup()
                old_checkpoint = await legacy.aput(
                    {"configurable": {"thread_id": investigation.id, "checkpoint_ns": ""}},
                    empty_checkpoint(),
                    {"source": "input", "step": -1},
                    {},
                )

            monkeypatch.setattr(db, "migration_files", lambda: through_sessions)
            assert sorted(await asyncio.gather(store.migrate(), other.migrate())) == [
                [],
                ["0012_sessions"],
            ]
            assert await store.migrate() == []
            await asyncio.gather(setup_checkpoints(store), setup_checkpoints(other))
            assert (await store.get_task(investigation.id)).lease_owner == "legacy-owner"
            assert (await store.get_investigation(investigation.id)).id == investigation.id
            assert (await store.get_proposal(proposal.id)).status == ProposalStatus.APPROVED
            assert await store.target_is_locked("legacy-key")
            async with store.transaction() as conn:
                assert (await (await conn.execute("SELECT id FROM sessions")).fetchall()) == []
            async with AsyncPostgresSaver.from_conn_string(session_dsn) as legacy:
                assert (await legacy.aget_tuple(old_checkpoint)).config == old_checkpoint

            monkeypatch.setattr(db, "migration_files", lambda: through_alert_inputs)
            assert (await store.migrate())[-1] == "0014_alert_input_names"
            async with store.transaction() as conn:
                await conn.execute(
                    """
                    INSERT INTO sessions (id, source, title, group_key) VALUES
                        ('legacy-user', 'user', '历史手工名称', NULL),
                        ('legacy-alert', 'alert', '历史告警名称', 'legacy-group')
                    """
                )
            monkeypatch.setattr(db, "migration_files", lambda: migrations)
            assert (await store.migrate())[0] == "0015_session_title"
            assert await store.migrate() == []
            sessions = await SessionStore(store).list_sessions()
            assert {row.id: (row.title, row.title_origin, row.last_seq) for row in sessions} == {
                "legacy-user": ("历史手工名称", "user", 0),
                "legacy-alert": ("历史告警名称", "alert", 0),
            }
            monkeypatch.setattr(
                cli, "Settings", lambda: Settings(_env_file=None, database_url=session_dsn)
            )
            assert await cli._run(Namespace(command="sessions", limit=50), False, None) == 0

    asyncio.run(scenario())
    output = capsys.readouterr().out
    assert "历史手工名称" in output and "历史告警名称" in output


def test_alert_group_serializes_first_delivery_and_close(session_dsn):
    async def scenario():
        async with Store.open(session_dsn) as store, Store.open(session_dsn) as other:
            await store.migrate()
            left, right = SessionStore(store), SessionStore(other)

            async def firing(sessions):
                async with sessions.alert_transaction("test-group") as conn:
                    return await sessions.record_alert(
                        conn,
                        "test-group",
                        title="告警",
                        text="告警细节",
                        display_text="告警摘要",
                        repeat_display_text="告警再次触发",
                    )

            receipts = await asyncio.gather(firing(left), firing(right))
            assert sorted(item.created for item in receipts) == [False, True]
            assert len({item.session_id for item in receipts}) == 1
            session_id = receipts[0].session_id
            assert await left.list_runnable_main() == [session_id]
            turns = await left.list_turns(session_id)
            assert len(turns) == 1 and turns[0].state == "queued"
            assert (await left.get_input(turns[0].first_input_id)).text == "告警细节"

            async with left.alert_transaction("test-group") as conn:
                locked = await left.get_open_alert_session(conn, "test-group")
                assert locked.id == session_id
                closed = await left.close_session(conn, session_id, reason="告警恢复")
                retry = asyncio.create_task(firing(right))
                assert await left.close_session(conn, session_id, reason="告警恢复") is None
            next_receipt = await retry
            assert next_receipt.created and next_receipt.session_id != session_id
            assert not await left.list_events(session_id, after_seq=closed.seq)
            assert (await left.get_session(session_id)).state == "closed"
            assert (await left.get_turn(turns[0].id)).state == "aborted"
            with pytest.raises(SessionClosed):
                await right.enqueue_input(session_id, "晚到", display_text="晚到")

    asyncio.run(scenario())


def test_enqueue_and_driver_events_share_atomic_sequence(session_dsn):
    async def scenario():
        async with Store.open(session_dsn) as store, Store.open(session_dsn) as other:
            await store.migrate()
            sessions, session, first, lease, turn = await _running(store)
            sender = SessionStore(other)

            async def add_input(index):
                return await sender.enqueue_input(
                    session.id,
                    f"线索 {index}",
                    display_text=f"线索 {index}",
                    input_id=f"line-{index}",
                )

            async with sessions.run(lease, turn.id) as tx:
                pending_enqueues = asyncio.gather(*(add_input(i) for i in (1, 2, 1, 3)))
                await tx.append_event("tool_call", {"tool": "query", "status": "ok"})
            inputs = await pending_enqueues

            retry = [item for item in inputs if item.id == "line-1"]
            assert retry[0] == retry[1]
            events = await sender.list_events(session.id, after_seq=first.queued_seq)
            assert [event.seq for event in events] == [3, 4, 5, 6]
            assert events[0].type == "tool_call"
            pending = await sessions.pending_inputs(lease, turn.id)
            assert [item.queued_seq for item in pending] == [2, 4, 5, 6]

    asyncio.run(scenario())


_LEASE_PROCESS = """
import asyncio
import sys
from aiops.db import Store
from aiops.sessions.store import SessionStore

async def main():
    async with Store.open(sys.argv[1]) as store:
        print("ready", flush=True)
        lease = await SessionStore(store).acquire_main_lease(sys.argv[2], sys.argv[3])
        print(lease.generation if lease else "blocked", flush=True)

asyncio.run(main())
"""


def test_two_processes_compete_for_one_generation(session_dsn):
    async def scenario():
        async with Store.open(session_dsn) as store:
            await store.migrate()
            sessions = SessionStore(store)
            session = await sessions.create_session(title="竞争")

            async def contender(owner):
                return await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-c",
                    _LEASE_PROCESS,
                    session_dsn,
                    session.id,
                    owner,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

            processes = []
            try:
                async with store.transaction() as conn:
                    await sessions.lock_session(conn, session.id)
                    processes.extend(await asyncio.gather(contender("p1"), contender("p2")))
                    async with asyncio.timeout(10):
                        for process in processes:
                            assert await process.stdout.readline() == b"ready\n"
                async with asyncio.timeout(10):
                    results = await asyncio.gather(
                        *(process.communicate() for process in processes)
                    )
                assert all(process.returncode == 0 for process in processes), results
                assert sorted(stdout.strip() for stdout, _ in results) == [b"1", b"blocked"]
            finally:
                for process in processes:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()

    asyncio.run(scenario())


_CHECKPOINT_PROCESS = """
import asyncio
import json
import sys
from aiops.db import Store
from aiops.sessions.checkpoint import SessionCheckpointer
from aiops.sessions.store import SessionStore

async def main():
    dsn, turn_id, owner, checkpoint_args, phase = sys.argv[1:]
    async with Store.open(dsn) as store:
        sessions = SessionStore(store)
        turn = await sessions.get_turn(turn_id)
        lease = await sessions.acquire_main_lease(turn.session_id, owner)
        saver = SessionCheckpointer(store, lease, turn)
        if phase == "before-commit":
            mark = saver._mark_delivered

            async def pause_after_write(*args):
                await mark(*args)
                print(phase, flush=True)
                await asyncio.Event().wait()

            saver._mark_delivered = pause_after_write
        await saver.aput(*json.loads(checkpoint_args))
        print(phase, flush=True)
        await asyncio.Event().wait()

asyncio.run(main())
"""


def test_checkpoint_delivery_rolls_back_and_retries_once(session_dsn):
    async def scenario():
        async with Store.open(session_dsn) as store, Store.open(session_dsn) as other:
            await store.migrate()
            await setup_checkpoints(store)
            sessions, session, first, lease, turn = await _running(store)
            checkpointer = SessionCheckpointer(store, lease, turn)
            observer = SessionCheckpointer(other, lease, turn)
            args = _checkpoint(checkpointer, turn, [first.id])

            async def crash(phase):
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-c",
                    _CHECKPOINT_PROCESS,
                    session_dsn,
                    turn.id,
                    lease.owner,
                    json.dumps(args),
                    phase,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    async with asyncio.timeout(10):
                        ready = await process.stdout.readline()
                        assert ready.rstrip() == phase.encode(), (
                            await process.stderr.read() if not ready else ready
                        )
                        if phase == "before-commit":
                            assert await observer.aget_tuple(args[0]) is None
                            assert (
                                await SessionStore(other).get_input(first.id)
                            ).checkpoint_id is None
                finally:
                    if process.returncode is None:
                        process.kill()
                    await process.wait()

            await crash("before-commit")
            assert await observer.aget_tuple(args[0]) is None
            assert [item.id for item in await sessions.pending_inputs(lease, turn.id)] == [first.id]
            assert [event.type for event in await sessions.list_events(session.id)] == [
                "session",
                "message",
            ]

            # 提交成功后终止进程，调用方用同一 checkpoint ID 重试。
            await crash("after-commit")
            saved = await observer.aput(*args)
            incoming = await sessions.get_input(first.id)
            assert incoming.checkpoint_id == saved["configurable"]["checkpoint_id"]
            assert incoming.delivered_step == -1
            assert await sessions.pending_inputs(lease, turn.id) == []
            events = await sessions.list_events(session.id, after_seq=first.queued_seq)
            assert [(event.seq, event.type) for event in events] == [(3, "input_delivered")]
            assert events[0].as_dict()["input_id"] == first.id

    asyncio.run(scenario())


class _GraphState(TypedDict, total=False):
    messages: list[str]
    vigil_input_ids: list[str]
    answer: str


def test_real_langgraph_persists_root_and_pending_writes(session_dsn):
    async def scenario():
        async with Store.open(session_dsn) as store:
            await store.migrate()
            await asyncio.gather(setup_checkpoints(store), setup_checkpoints(store))
            sessions, session, first, lease, turn = await _running(store)
            saver = SessionCheckpointer(store, lease, turn)

            async def answer(state):
                delivery = await sessions.get_input(first.id)
                assert delivery.checkpoint_id is not None
                saved = await saver.aget_tuple(
                    {
                        "configurable": {
                            "thread_id": turn.checkpoint_thread_id,
                            "checkpoint_id": delivery.checkpoint_id,
                        }
                    }
                )
                assert delivery.delivered_step == saved.metadata["step"]
                assert saved.metadata[TURN_ID_METADATA] == turn.id
                return {"answer": f"回应：{state['messages'][0]}"}

            builder = StateGraph(_GraphState)
            builder.add_node("answer", answer)
            builder.add_edge(START, "answer")
            builder.add_edge("answer", END)
            graph = builder.compile(checkpointer=saver)
            config = {"configurable": {"thread_id": turn.checkpoint_thread_id}}
            result = await graph.ainvoke(
                {"messages": [first.text], INPUT_IDS_CHANNEL: [first.id]},
                config,
                durability="sync",
            )
            assert result["answer"] == "回应：原文"
            assert (await graph.aget_state(config)).values["answer"] == "回应：原文"

            async with store.transaction() as conn:
                writes = await conn.execute(
                    "SELECT channel FROM checkpoint_writes WHERE thread_id = %s",
                    (turn.checkpoint_thread_id,),
                )
                assert "answer" in {row["channel"] for row in await writes.fetchall()}
            events = [event.as_dict() for event in await sessions.list_events(session.id)]
            assert events[1]["text"] == "显示文本"
            assert "原文" not in repr(events)

    asyncio.run(scenario())


async def _expire_main(store, session_id):
    async with store.transaction() as conn:
        await conn.execute(
            "UPDATE sessions SET lease_expires_at = clock_timestamp() - interval '1 second' "
            "WHERE id = %s",
            (session_id,),
        )


def test_renewal_then_takeover_fences_checkpoint_writes_events_and_ledger(session_dsn):
    async def scenario():
        async with Store.open(session_dsn) as store, Store.open(session_dsn) as successor:
            await store.migrate()
            await setup_checkpoints(store)
            sessions, session, first, original, turn = await _running(store)
            old_saver = SessionCheckpointer(store, original, turn)
            args = _checkpoint(old_saver, turn, [first.id])
            saved = await old_saver.aput(*args)
            renewed = await sessions.renew_lease(original)
            assert renewed.generation == original.generation
            assert renewed.expires_at > original.expires_at
            await old_saver.aput_writes(saved, [("messages", ["旧代续期后暂存"])], "known")
            saved = await old_saver.aput(*_checkpoint(old_saver, turn, [first.id], parent=saved))

            incident, investigation = _investigation()
            async with sessions.run(original, turn.id) as tx:
                await tx.ledger.save_incident(incident)
                await tx.ledger.create_investigation(investigation)
                await tx.ledger.create_task(
                    InvestigationTask(
                        investigation_id=investigation.id,
                        phase=TaskPhase.RUNNING,
                        runtime_version="session",
                        calendar_started_at=investigation.started_at,
                    )
                )
                assert (await tx.ledger.get_investigation(investigation.id)).id == investigation.id

            old_ledger = store.for_turn(original, turn.id)
            await old_ledger.save_hypothesis(
                Hypothesis(id="hyp-old", investigation_id=investigation.id, statement="假设")
            )
            await old_ledger.start_tool_request(
                ToolRequest(
                    investigation_id=investigation.id,
                    tool_call_id="request",
                    tool="read",
                    state=ToolRequestState.RUNNING,
                    started_at=datetime.now(UTC),
                )
            )

            await _expire_main(store, session.id)
            new_sessions = SessionStore(successor)
            assert (await new_sessions.get_session(session.id)).state == "interrupted"
            assert await new_sessions.list_runnable_main() == [session.id]
            with pytest.raises(LeaseLost):
                await sessions.renew_lease(original)

            # 即便接手恰好复用了标识，旧代次也不可写。
            new_lease = await new_sessions.acquire_main_lease(session.id, original.owner)
            assert new_lease.generation == original.generation + 1
            assert await new_sessions.start_main_turn(new_lease) == turn
            assert not await sessions.release_lease(original)
            with pytest.raises(LeaseLost):
                await old_saver.aput(*_checkpoint(old_saver, turn, [], parent=saved))
            with pytest.raises(LeaseLost):
                await old_saver.aput_writes(saved, [("messages", ["迟到"])], "late")
            with pytest.raises(LeaseLost):
                async with sessions.run(original, turn.id) as tx:
                    await tx.append_event("message", {"role": "assistant", "text": "迟到"})
            with pytest.raises(LeaseLost):
                await old_ledger.set_hypothesis_status("hyp-old", "refuted")
            with pytest.raises(LeaseLost):
                await old_ledger.finish_tool_request(
                    investigation.id, "request", state=ToolRequestState.OBSERVED, result={"late": 1}
                )
            with pytest.raises(LeaseLost):
                await old_ledger.set_investigation_status(
                    investigation.id, InvestigationStatus.ABORTED, finished_at=datetime.now(UTC)
                )
            with pytest.raises(LeaseLost):
                await store.add_usage(investigation.id, tool_calls=1)
            with pytest.raises(LeaseLost):
                await store.acquire_task_lease(investigation.id, "legacy")

            new_saver = SessionCheckpointer(successor, new_lease, turn)
            resuming = await new_saver.aget_tuple(args[0])
            assert resuming.config == saved
            assert all(task_id != "late" for task_id, _, _ in resuming.pending_writes)
            assert (await successor.get_tool_request(investigation.id, "request")).result is None

            with pytest.raises(RuntimeError, match="回滚组合写入"):
                async with new_sessions.run(new_lease, turn.id) as tx:
                    await tx.ledger.add_usage(investigation.id, tool_calls=1)
                    await tx.append_event("tool_call", {"tool": "read"})
                    raise RuntimeError("回滚组合写入")
            assert (await successor.get_investigation(investigation.id)).usage.tool_calls == 0
            assert len(await new_sessions.list_events(session.id)) == 3

            async with new_sessions.run(new_lease, turn.id) as tx:
                await tx.ledger.finish_tool_request(
                    investigation.id, "request", state=ToolRequestState.OBSERVED, result={"ok": 1}
                )
                await tx.ledger.set_investigation_status(
                    investigation.id,
                    InvestigationStatus.COMPLETED,
                    conclusion="正常",
                    finished_at=datetime.now(UTC),
                )
                await tx.append_event("result", {"status": "completed", "summary": "正常"})
                await tx.set_turn_state("completed")
            assert await successor.get_investigation_id_for_turn(turn.id) == investigation.id
            assert (await successor.get_investigation(investigation.id)).conclusion == "正常"
            assert (await new_sessions.get_session(session.id)).state == "idle"
            assert [event.seq for event in await new_sessions.list_events(session.id)] == [
                1,
                2,
                3,
                4,
            ]

    asyncio.run(scenario())


def test_side_turn_owns_its_input_and_checkpoint_across_main_resume(session_dsn):
    async def scenario():
        async with Store.open(session_dsn) as store:
            await store.migrate()
            await setup_checkpoints(store)
            sessions, session, first, main, turn = await _running(store)
            main_saver = SessionCheckpointer(store, main, turn)
            await main_saver.aput(*_checkpoint(main_saver, turn, [first.id]))
            async with sessions.run(main, turn.id) as tx:
                await tx.set_turn_state("awaiting_approval")
            side_input = await sessions.enqueue_input(session.id, "旁路", display_text="旁路")
            assert await sessions.list_runnable_side() == [session.id]
            side = await sessions.acquire_side_lease(session.id, "side")
            assert await sessions.list_runnable_side() == []
            side_turn = await sessions.get_turn(side.side_turn_id)
            side_saver = SessionCheckpointer(store, side, side_turn)
            assert side_turn.checkpoint_thread_id != turn.checkpoint_thread_id
            assert side_turn.first_input_id == side_input.id

            with pytest.raises(LeaseLost):
                async with store.transaction() as conn:
                    await sessions.require_lease(conn, side, main_only=True)
            with pytest.raises(LeaseLost):
                async with sessions.run(side, turn.id):
                    pytest.fail("旁路越权写入主轮")
            with pytest.raises(LeaseLost):
                await side_saver.aput_writes(
                    {"configurable": {"thread_id": turn.checkpoint_thread_id}},
                    [],
                    "wrong-thread",
                )
            async with sessions.run(side, side_turn.id) as tx:
                with pytest.raises(ValueError):
                    await tx.set_turn_state("awaiting_approval")
            assert (await sessions.get_session(session.id)).state == "awaiting_approval"

            follow_up = await sessions.enqueue_input(session.id, "主线", display_text="主线")
            async with sessions.run(main, turn.id) as tx:
                await tx.set_turn_state("running")
            with pytest.raises(ValueError, match="排队顺序"):
                await main_saver.aput(*_checkpoint(main_saver, turn, [side_input.id]))
            assert [item.id for item in await sessions.pending_inputs(main, turn.id)] == [
                follow_up.id
            ]
            await side_saver.aput(*_checkpoint(side_saver, side_turn, [side_input.id]))
            async with sessions.run(side, side_turn.id) as tx:
                await tx.append_event("message", {"role": "assistant", "text": "旁路回答"})
                await tx.set_turn_state("completed")
            assert (await sessions.get_session(session.id)).state == "working"
            assert await sessions.acquire_side_lease(session.id, "idle-side") is None
            async with sessions.run(main, turn.id) as tx:
                await tx.set_turn_state("completed")
            assert (await sessions.start_main_turn(main)).first_input_id == follow_up.id
            assert (await sessions.get_input(side_input.id)).turn_id == side_turn.id

    asyncio.run(scenario())


@pytest.mark.parametrize("manual_first", [False, True])
def test_title_transaction_serializes_rename_and_first_message(session_dsn, manual_first):
    async def scenario():
        async with Store.open(session_dsn) as store, Store.open(session_dsn) as other:
            await store.migrate()
            sessions, concurrent = SessionStore(store), SessionStore(other)
            initial = await sessions.create_session(title="新会话", title_origin="pending")
            secret = "sk-fixture-example-key"
            question = f"  核对 token={secret}   " + "字" * 125

            async def send():
                return await concurrent.enqueue_input(
                    initial.id, question, display_text=question, input_id="first-title-input"
                )

            async with store.transaction() as conn:
                locked = await sessions.lock_session(conn, initial.id)
                if manual_first:
                    pending = asyncio.create_task(send())
                    await sessions._set_title(conn, locked, "人工选择", "user")
                else:
                    pending = asyncio.create_task(concurrent.rename_session(initial.id, "人工选择"))
                    original = await sessions._enqueue_input(
                        conn, initial.id, question, question, "first-title-input"
                    )
            result = await pending
            events = await sessions.list_events(initial.id)
            titles = [event.payload["title"] for event in events if event.payload.get("renamed")]
            if manual_first:
                assert titles == ["人工选择"]
                original = result
            else:
                automatic, manual = titles
                assert len(automatic) == 120 and automatic.endswith("…")
                assert automatic.startswith("核对 token=[已遮盖] ")
                assert secret not in automatic and manual == "人工选择"
            # 重试仍只得到同一输入，标题事件和消息序号都稳定。
            assert await send() == original
            assert await sessions.list_events(initial.id) == events
            final = await sessions.get_session(initial.id)
            assert (final.title, final.title_origin, final.last_seq) == (
                "人工选择",
                "user",
                events[-1].seq,
            )

    asyncio.run(scenario())
