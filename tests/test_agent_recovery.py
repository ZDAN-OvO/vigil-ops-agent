"""恢复与故障边界：关闭、未发送意图、查询重放和动作回执。"""

import asyncio
import json
from datetime import UTC, datetime

import pytest
from db_env import DSN
from investigation_support import action_case, agent_case, report_call
from test_agent_actions import (
    _assert_sent,
    _decide,
    _drive,
    _investigation_id,
    _paused,
    _preamble,
    _valid_report,
)

from aiops.actions.policy import target_key
from aiops.actions.rollout_undo import ActionError
from aiops.actions.safety import ActionSafety
from aiops.agent import tools as agent_tools
from aiops.agent.conversation import AwaitingActionReceipt
from aiops.agent.engine import LeaseConflict, TaskNotResumable
from aiops.contracts import (
    AttemptStatus,
    InvestigationTask,
    ProposalStatus,
    TaskPhase,
    ToolRequestState,
)
from aiops.db import Store
from aiops.sessions.models import LeaseLost, SessionClosed

pytestmark = pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL，跳过真实库测试")


def test_cancel_before_start_aborts_without_model_calls(ids):
    async def run():
        async with agent_case(ids, responses=[]) as case:
            await case.store.create_task(
                InvestigationTask(
                    investigation_id=ids["investigation"],
                    phase=TaskPhase.RUNNING,
                    runtime_version="langchain-agent-1",
                    calendar_started_at=datetime.now(UTC),
                )
            )
            await case.store.request_cancel(ids["investigation"], at=datetime.now(UTC))
            report = await case.runtime.run()
            return report, len(case.model.seen), case

    report, calls, case = asyncio.run(run())
    assert report.task["outcome"] == "aborted"
    assert report.task["termination_code"] == "cancelled"
    assert calls == 0


def test_lease_conflict_blocks_concurrent_driver(ids):
    async def run():
        async with agent_case(ids, responses=[]) as case:
            await case.store.create_task(
                InvestigationTask(
                    investigation_id=ids["investigation"],
                    phase=TaskPhase.RUNNING,
                    runtime_version="langchain-agent-1",
                    calendar_started_at=datetime.now(UTC),
                )
            )
            assert await case.store.acquire_task_lease(
                ids["investigation"], "other-process", seconds=300
            )
            try:
                await case.runtime.run()
                return None
            except LeaseConflict as exc:
                return str(exc)

    message = asyncio.run(run())
    assert message is not None and "驱动者" in message


def test_resume_after_terminal_task_is_refused(ids):
    async def run():
        async with agent_case(
            ids, responses=[report_call(confirmed=False, conclusion="只读结束")]
        ) as case:
            await case.runtime.run()
            with pytest.raises(TaskNotResumable):
                await case.runtime.resume(
                    proposal_id="act-does-not-exist", decision="approved", actor="operator"
                )

    asyncio.run(run())


@pytest.mark.parametrize("approved", [False, True])
def test_close_while_waiting_ends_turn_without_write(tmp_path, action_session_ids, approved):
    async def run():
        async with action_case(
            tmp_path,
            action_session_ids,
            responses=_preamble(),
            namespace="vigil-shop",
        ) as case:
            pause, proposal = await _paused(case)
            if approved:
                await _decide(case.safety, pause.card)
            calls_before_close = len(case.model.seen)
            async with Store.open(DSN) as service, service.transaction() as conn:
                closed = await ActionSafety(service).close_session(
                    conn, case.session_id, reason="用户取消"
                )
            assert closed.type == "session_closed"
            turn = await case.driver.sessions.get_turn(pause.card.turn_id)
            assert turn.state == "aborted"
            assert turn.finished_at is not None
            assert (await case.driver.sessions.get_session(case.session_id)).state == "closed"
            with pytest.raises(SessionClosed):
                await _drive(case)
            with pytest.raises(ActionError) as caught:
                await _decide(case.safety, pause.card)
            assert caught.value.code == "session_closed"
            assert len(case.model.seen) == calls_before_close
            assert await case.store.get_attempt(proposal.id) is None
            assert (await case.store.get_proposal(proposal.id)).status is ProposalStatus.VOIDED
            authorization = await case.store.get_approval(proposal.id)
            if approved:
                assert authorization.revoked_at is not None
            else:
                assert authorization is None
            (event,) = await case.events("action_result")
            assert pause.card.seq < closed.seq < event.seq
            assert event.turn_id == pause.card.turn_id
            assert event.payload["card_id"] == proposal.id
            assert event.payload["proposal_status"] == "voided"
            assert event.payload["written"] is False
            assert case.restart.deleted == []

    asyncio.run(run())


class SimulatedDriverExit(BaseException):
    """模拟进程在事务提交后退出，让后续驱动接手。"""


@pytest.mark.parametrize("may_have_sent", [False, True], ids=["intent", "send-granted"])
def test_takeover_converges_attempt_without_replay(
    tmp_path, action_session_ids, monkeypatch, may_have_sent
):
    boundary = "grant_send" if may_have_sent else "save_intent"
    original = getattr(ActionSafety, boundary)

    async def exit_after_commit(self, *args):
        await original(self, *args)
        raise SimulatedDriverExit

    monkeypatch.setattr(ActionSafety, boundary, exit_after_commit)

    async def run():
        async with action_case(
            tmp_path,
            action_session_ids,
            responses=[*_preamble(), _valid_report],
            namespace="vigil-shop",
        ) as case:
            pause, proposal = await _paused(case)
            await _decide(case.safety, pause.card)
            with pytest.raises(SimulatedDriverExit):
                await _drive(case)
            initial = await case.store.get_attempt(proposal.id)
            assert initial.status is (
                AttemptStatus.EXECUTING if may_have_sent else AttemptStatus.INTENT
            )
            assert (initial.send_granted_at is not None) is may_have_sent
            assert case.restart.dry_runs == [proposal.target.uid]
            assert case.restart.deleted == []

            if may_have_sent:
                model_calls = len(case.model.seen)
                with pytest.raises(AwaitingActionReceipt, match=initial.id):
                    await _drive(case)
                assert len(case.model.seen) == model_calls
                assert case.restart.deleted == []
                # 模拟原发送者停止续锁，接手后应记录未知回执。
                async with case.store.transaction() as conn:
                    await conn.execute(
                        "UPDATE action_locks SET expires_at = clock_timestamp() "
                        "WHERE proposal_id = %s",
                        (proposal.id,),
                    )

            done = await _drive(case)
            assert done.exit_code == 0
            assert done.result.turn_id == pause.card.turn_id
            assert case.restart.dry_runs == [proposal.target.uid]
            assert case.restart.deleted == []
            target = proposal.target
            key = target_key(target.cluster, target.namespace, target.kind, target.name)
            recovered_status = AttemptStatus.UNKNOWN if may_have_sent else AttemptStatus.VOIDED
            recovered_proposal = ProposalStatus.UNKNOWN if may_have_sent else ProposalStatus.VOIDED
            assert (await case.store.get_proposal(proposal.id)).status is recovered_proposal
            assert await case.safety.target_is_locked(key) is may_have_sent
            receipt = await case.store.get_attempt(proposal.id)
            assert receipt.id == initial.id
            assert receipt.send_granted_at == initial.send_granted_at
            assert receipt.status is recovered_status
            if not may_have_sent:
                assert (await case.store.get_approval(proposal.id)).revoked_at is not None

            investigation_id = _investigation_id(done)
            assert await case.store.count_attempts_for_investigation(investigation_id) == 1
            request = await case.store.get_tool_request(investigation_id, "call-action")
            assert request.state is ToolRequestState.ACTION_EXECUTED
            assert request.action_attempt_id == receipt.id
            feedback = json.loads(request.result["tool_message"])
            assert feedback["status"] == "replayed"
            assert feedback["attempt_id"] == receipt.id
            written = None if may_have_sent else False
            assert feedback["write_sent"] is written
            events = await case.events("action_result")
            assert [event.payload["status"] for event in events] == (
                ["intent", "executing", "unknown"] if may_have_sent else ["intent", "voided"]
            )
            assert all(event.payload["attempt_id"] == receipt.id for event in events)
            assert all(event.turn_id == pause.card.turn_id for event in events)
            terminal = events[-1]
            assert terminal.payload["written"] is written
            assert terminal.payload["proposal_status"] == recovered_proposal
            assert terminal.seq < done.result.seq

    asyncio.run(run())


def test_query_replays_saved_observation_before_action(tmp_path, action_session_ids, monkeypatch):
    original = agent_tools._finish_request
    interrupted_observation = None

    async def exit_after_observation(context, call_id, **fields):
        nonlocal interrupted_observation
        await original(context, call_id, **fields)
        if call_id == "call-pods" and interrupted_observation is None:
            interrupted_observation = fields["observation_record"].id
            raise LeaseLost("模拟观测提交后、图保存工具回复前失租")

    monkeypatch.setattr(agent_tools, "_finish_request", exit_after_observation)

    async def run():
        async with action_case(
            tmp_path,
            action_session_ids,
            responses=_preamble(),
            namespace="vigil-shop",
        ) as case:
            with pytest.raises(LeaseLost, match="图保存工具回复前"):
                await _drive(case)
            assert interrupted_observation is not None
            assert [request.tool for request in case.toolbox.requests] == ["kubernetes.list_pods"]
            pause, proposal = await _paused(case)
            investigation_id = _investigation_id(pause)
            observations = await case.store.list_observations(investigation_id)
            assert len(observations) == 2
            assert interrupted_observation in {item.id for item in observations}
            assert interrupted_observation in proposal.evidence_ids
            request = await case.store.get_tool_request(investigation_id, "call-pods")
            assert request.state is ToolRequestState.OBSERVED
            assert json.loads(request.result["tool_message"])["observation_id"] == (
                interrupted_observation
            )
            assert [request.tool for request in case.toolbox.requests] == [
                "kubernetes.list_pods",
                "kubernetes.list_logs",
            ]
            query_events = [
                event
                for event in await case.events("tool_call")
                if event.payload["tool"] == "kubernetes.list_pods"
            ]
            assert [event.payload["status"] for event in query_events] == ["running", "ok"]
            assert query_events[-1].payload["observation_id"] == interrupted_observation
            assert query_events[-1].turn_id == pause.card.turn_id
            assert query_events[-1].seq < pause.card.seq
            task = await case.store.get_task(investigation_id)
            assert task.used_tool_calls == 5

    asyncio.run(run())


def test_replayed_action_request_returns_saved_feedback(tmp_path, action_session_ids, monkeypatch):
    """真实执行已落回执、工具尚未落反馈，恢复使用原尝试。"""
    original = agent_tools._finish_request
    interrupted_attempt = None

    async def exit_before_feedback(context, call_id, **fields):
        nonlocal interrupted_attempt
        if call_id == "call-action" and fields.get("action_attempt_id") and not interrupted_attempt:
            interrupted_attempt = fields["action_attempt_id"]
            raise LeaseLost("模拟已发送并记录回执后失租")
        return await original(context, call_id, **fields)

    monkeypatch.setattr(agent_tools, "_finish_request", exit_before_feedback)

    async def run():
        def report_on_receipt(messages):
            message = json.loads(messages[-1].content)
            assert message["status"] == "replayed"
            assert message["attempt_id"] == interrupted_attempt
            assert message["write_sent"] is True
            return _valid_report(messages)

        async with action_case(
            tmp_path,
            action_session_ids,
            responses=[*_preamble(), report_on_receipt],
        ) as case:
            with pytest.raises(LeaseLost, match="回执后失租"):
                await _drive(case)
            assert interrupted_attempt is not None
            before_events = await case.events("action_result")
            terminal = before_events[-1]
            assert terminal.payload["attempt_id"] == interrupted_attempt
            assert terminal.payload["written"] is True
            investigation_id = await case.store.get_investigation_id_for_turn(terminal.turn_id)
            waiting = await case.store.get_tool_request(investigation_id, "call-action")
            assert waiting.state is ToolRequestState.ACTION_PENDING
            saved = await case.store.get_attempt(waiting.proposal_id)
            assert saved.id == interrupted_attempt
            assert saved.status is AttemptStatus.VERIFIED
            assert saved.send_granted_at is not None

            done = await _drive(case)
            assert done.exit_code == 0
            assert _investigation_id(done) == investigation_id
            proposal, attempt = await _assert_sent(case, done)
            assert proposal.id == waiting.proposal_id
            assert attempt == saved
            assert await case.events("action_result") == before_events

    asyncio.run(run())
