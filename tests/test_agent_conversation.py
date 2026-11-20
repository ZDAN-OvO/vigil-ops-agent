"""用真实 PostgreSQL 和 LangGraph 验证会话，在模型和集群 I/O 边界用替身。"""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from db_env import DSN
from investigation_support import FakeAgentExecutor, FakeToolbox, ScriptedChatModel, settings
from langchain_core.messages import AIMessage, HumanMessage
from test_agent_actions import _approved
from test_agent_recovery import _preamble, _valid_report

from aiops.agent import tools as agent_tools
from aiops.agent.conversation import ConversationDriver, redact_text
from aiops.agent.engine import LangChainInvestigation
from aiops.contracts import ApprovalDecision, ProposalStatus, ToolRequestState
from aiops.db import Store
from aiops.sessions.checkpoint import SessionCheckpointer, setup_checkpoints
from aiops.sessions.models import LeaseLost

pytestmark = pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL")

DEPLOY_TOOL = "kubernetes.list_deployments"


def deploys(message_id="deployment-query"):
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "kubernetes_list_deployments",
                "args": {"selector": "app=checkout"},
                "id": message_id,
            },
        ],
    )


@asynccontextmanager
async def case(tmp_path, *, responses=(), toolbox=None, executor_factory=None, **config):
    async with Store.open(DSN) as store:
        await store.migrate()
        await setup_checkpoints(store)
        scripted = ScriptedChatModel(responses=list(responses))
        driver = ConversationDriver(
            settings=settings(runtime_dir=tmp_path, **config),
            store=store,
            toolbox=toolbox or FakeToolbox(),
            executor_factory=executor_factory,
            model=scripted,
        )
        session = await driver.sessions.create_session(title="会话测试")
        yield driver, session.id


async def enqueue(driver, session_id, text):
    return await driver.sessions.enqueue_input(session_id, text, display_text=redact_text(text))


def test_queries_again_next_turn_and_projects_only_public_fields(tmp_path):
    async def run():
        toolbox = FakeToolbox()
        async with case(
            tmp_path,
            toolbox=toolbox,
            responses=[
                deploys(),
                AIMessage(content="当前 2 个"),
                deploys(),
                AIMessage(content="当前 3 个，token=demo-secret /home/demo/credentials"),
            ],
        ) as (driver, session_id):
            toolbox.responses[DEPLOY_TOOL] = {"data": {"items": [{"replicas": 2}]}}
            first = await enqueue(driver, session_id, "现在有几个副本？")
            async with driver.claim(session_id) as main:
                one = await main.drive()
                assert one.exit_code == 0 and one.result.payload["summary"] == "当前 2 个"
                toolbox.responses[DEPLOY_TOOL] = {"data": {"items": [{"replicas": 3}]}}
                second = await enqueue(driver, session_id, "现在呢？")
                two = await main.drive()
                assert two.exit_code == 0
                assert await main.drive() is None
            assert len(toolbox.requests) == 2
            assert '"replicas": 3' in json.loads(driver.model.seen[-1][-1].content)["observation"]
            events = [event.as_dict() for event in await driver.sessions.list_events(session_id)]
            assert "demo-secret" not in str(events)
            assert "/home/demo/credentials" not in str(events)
            assert "raw_ref" not in str(events)
            for incoming in (first, second):
                saved = await driver.sessions.get_input(incoming.id)
                assert saved.checkpoint_id is not None
                delivered = [
                    evt
                    for evt in events
                    if evt["type"] == "input_delivered" and evt["input_id"] == incoming.id
                ]
                assert len(delivered) == 1

    asyncio.run(run())


class BlockingToolbox(FakeToolbox):
    def __init__(self):
        super().__init__()
        self.called = asyncio.Event()
        self.reply = asyncio.Event()

    async def run(self, context, time_range, request):
        self.called.set()
        await self.reply.wait()
        return await super().run(context, time_range, request)


def test_interjection_is_persisted_before_next_model_and_late_input_starts_next_turn(tmp_path):
    async def run():
        toolbox = BlockingToolbox()
        finish_model = asyncio.Event()
        async with case(tmp_path, toolbox=toolbox) as (driver, session_id):
            first = await enqueue(driver, session_id, "查看副本")
            driver.model.responses[:] = [
                deploys(),
                AIMessage(content="本轮回复"),
                AIMessage(content="下一轮回复"),
            ]
            original = driver.model.__class__._agenerate

            async def barrier(model, messages, *args, **kwargs):
                if len(model.seen) == 1:
                    assert await driver.sessions.get_input(early.id) is not None
                    assert (await driver.sessions.get_input(early.id)).delivered_step is not None
                    assert any(message.content == "运行中线索" for message in messages)
                    finish_model.set()
                    await model_continue.wait()
                return await original(model, messages, *args, **kwargs)

            model_continue = asyncio.Event()
            with pytest.MonkeyPatch.context() as monkeypatch:
                monkeypatch.setattr(ScriptedChatModel, "_agenerate", barrier)
                async with driver.claim(session_id) as main:
                    task = asyncio.create_task(main.drive())
                    await asyncio.wait_for(toolbox.called.wait(), 5)
                    early = await enqueue(driver, session_id, "运行中线索")
                    toolbox.reply.set()
                    await asyncio.wait_for(finish_model.wait(), 5)
                    late = await enqueue(driver, session_id, "模型已经开始后的消息")
                    model_continue.set()
                    one = await task
                    saved = await driver.sessions.get_input(first.id)
                    assert saved.turn_id == (await driver.sessions.get_input(early.id)).turn_id
                    assert (await driver.sessions.get_input(late.id)).turn_id is None
                    two = await main.drive()
                    assert one.result.turn_id != two.result.turn_id
                    assert (await driver.sessions.get_input(late.id)).turn_id == two.result.turn_id

    asyncio.run(run())


class CardExecutor(FakeAgentExecutor):
    def __init__(self, driver, lease, turn_id, failures, sent):
        super().__init__(driver.store)
        self.driver = driver
        self.lease = lease
        self.turn_id = turn_id
        self.failures = failures
        self.sent = sent

    async def get_card_decision(self, proposal_id, *, session_id):
        assert self.lease.session_id == session_id
        return await self.driver.store.get_approval(proposal_id)

    async def publish_card(self, proposal_id, *, lease, turn_id):
        assert lease == self.lease and turn_id == self.turn_id
        turn = await self.driver.sessions.get_turn(turn_id)
        saved = await SessionCheckpointer(self.driver.store, lease, turn).aget_tuple(
            {"configurable": {"thread_id": turn.checkpoint_thread_id}}
        )
        assert any(channel == "__interrupt__" for _, channel, _ in saved.pending_writes)
        if self.failures:
            raise self.failures.pop()
        async with self.driver.sessions.run(lease, turn_id) as tx:
            found = next(
                (
                    event
                    for event in await self.driver.sessions.list_events(lease.session_id)
                    if event.type == "approval_required" and event.payload["card_id"] == proposal_id
                ),
                None,
            )
            if found:
                return found
            return await tx.append_event(
                "approval_required",
                {
                    "card_id": proposal_id,
                    "action": "restart_pod",
                    "target": "vigil-shop/target-pod",
                    "risk": "approval",
                    "url": None,
                },
            )

    async def execute_authorized(self, proposal_id, *, actor):
        result = await super().execute_authorized(proposal_id, actor=actor)
        self.sent.append(proposal_id)
        async with self.driver.sessions.run(self.lease, self.turn_id) as tx:
            await tx.append_event(
                "action_result",
                {
                    "card_id": proposal_id,
                    "attempt_id": result.id,
                    "written": True,
                    "verification": {},
                },
            )
        return result


async def persist_decision(proposal_id, *, rejected=False):
    async with Store.open(DSN) as approver:
        proposal = await approver.get_proposal(proposal_id)
        approval = _approved(proposal)
        if rejected:
            approval = approval.model_copy(update={"decision": ApprovalDecision.REJECTED})
        async with approver.transaction() as conn:
            await conn.execute(
                "UPDATE action_proposals SET status = %s WHERE id = %s",
                ("rejected" if rejected else "approved", proposal_id),
            )
            await approver._insert_approval(conn, approval)


@pytest.mark.parametrize("rejected", [False, True])
def test_saved_interrupt_republishes_and_resumes_from_persisted_decision(tmp_path, rejected):
    async def run():
        failures = [RuntimeError("模拟 checkpoint 成功与发布之间崩溃")]
        sent = []

        @asynccontextmanager
        async def executor_factory(*, lease, turn_id):
            assert lease.side_turn_id is None
            yield CardExecutor(driver, lease, turn_id, failures, sent)

        async with case(
            tmp_path,
            responses=_preamble(),
            executor_factory=executor_factory,
            namespace="vigil-shop",
        ) as (driver, session_id):
            await enqueue(driver, session_id, "排查异常并提出动作")
            async with driver.claim(session_id) as main:
                with pytest.raises(RuntimeError, match="之间崩溃"):
                    await main.drive()
            assert not [
                event
                for event in await driver.sessions.list_events(session_id)
                if event.type == "approval_required"
            ]
            assert (await driver.sessions.get_session(session_id)).state == "interrupted"
            async with driver.claim(session_id) as main:
                pause = await main.drive()
                assert pause.exit_code == 3 and pause.card.type == "approval_required"
                assert (await main.drive()).card.seq == pause.card.seq
                proposal_id = pause.card.payload["card_id"]
                investigation_id = await driver.store.get_investigation_id_for_turn(
                    pause.result.turn_id
                )
                first_usage = await driver.store.get_task(investigation_id)
                await enqueue(driver, session_id, "侧线问答，不变更卡片")
                driver.model.responses[:] = [deploys(), AIMessage(content="旁路回复")]
                async with driver.claim(session_id, lane="side") as side:
                    side_answer = await side.drive()
                assert side_answer.exit_code == 3
                assert side_answer.result.payload["summary"] == "旁路回复"
                assert not sent
                assert (await driver.store.get_task(investigation_id)).used_model_calls == (
                    first_usage.used_model_calls
                )
                await persist_decision(proposal_id, rejected=rejected)

                def report_with_side(messages):
                    assert any(
                        isinstance(m, HumanMessage) and "旁路回复" in m.content for m in messages
                    )
                    return _valid_report(messages)

                driver.model.responses[:] = [report_with_side]
                done = await main.drive()
            assert done.exit_code == 0
            assert sent == ([] if rejected else [proposal_id])
            events = await driver.sessions.list_events(session_id)
            assert sum(event.type == "approval_required" for event in events) == 1
            assert sum(event.type == "action_result" for event in events) == int(not rejected)
            assert (await driver.sessions.get_session(session_id)).state == "idle"

    asyncio.run(run())


async def expire(store, session_id):
    async with store.transaction() as conn:
        await conn.execute(
            "UPDATE sessions SET lease_expires_at = clock_timestamp() - interval '1 second' "
            "WHERE id = %s",
            (session_id,),
        )


def test_takeover_retries_unrecorded_query_and_discards_old_reply(tmp_path):
    async def run():
        toolbox = BlockingToolbox()
        async with case(tmp_path, responses=[deploys()], toolbox=toolbox) as (driver, session_id):
            await enqueue(driver, session_id, "排查副本")
            async with Store.open(DSN) as other:
                new_toolbox = FakeToolbox()
                new = ConversationDriver(
                    settings=driver.settings,
                    store=other,
                    toolbox=new_toolbox,
                    model=ScriptedChatModel(responses=[AIMessage(content="接手完成")]),
                )
                async with driver.claim(session_id) as old:
                    running = asyncio.create_task(old.drive())
                    await asyncio.wait_for(toolbox.called.wait(), 5)
                    async with new.claim(session_id) as busy:
                        assert busy is None
                    await expire(other, session_id)
                    async with new.claim(session_id) as takeover:
                        assert takeover.lease.generation == old.lease.generation + 1
                        result = await takeover.drive()
                        toolbox.reply.set()
                        with pytest.raises(LeaseLost):
                            await running
            assert result.exit_code == 0 and len(new_toolbox.requests) == 1
            investigation_id = result.result.payload["investigation_id"]
            assert len(await driver.store.list_observations(investigation_id)) == 1
            assert (await driver.store.get_task(investigation_id)).used_tool_calls == 1
            events = await driver.sessions.list_events(session_id)
            assert [e.payload["status"] for e in events if e.type == "tool_call"] == [
                "running",
                "ok",
            ]

    asyncio.run(run())


def test_takeover_replays_saved_tool_result_after_pending_write_failure(tmp_path, monkeypatch):
    async def run():
        crashed = False
        original = agent_tools._finish_request

        async def fail_on_result(context, call_id, **fields):
            nonlocal crashed
            await original(context, call_id, **fields)
            if not crashed and fields.get("observation_record") is not None:
                crashed = True
                raise LeaseLost("模拟查询已提交但 ToolMessage pending write 未保存")

        monkeypatch.setattr(agent_tools, "_finish_request", fail_on_result)
        toolbox = FakeToolbox()
        async with case(tmp_path, toolbox=toolbox, responses=[deploys()]) as (driver, session_id):
            await enqueue(driver, session_id, "查一次")
            async with driver.claim(session_id) as main:
                with pytest.raises(LeaseLost):
                    await main.drive()
            assert crashed and len(toolbox.requests) == 1
            driver.model.responses[:] = [AIMessage(content="已读取原观测")]
            async with driver.claim(session_id) as main:
                result = await main.drive()
            assert len(toolbox.requests) == 1
            assert result.exit_code == 0
            investigation_id = result.result.payload["investigation_id"]
            assert len(await driver.store.list_observations(investigation_id)) == 1

    asyncio.run(run())


def test_stale_report_is_fenced_after_takeover(tmp_path, monkeypatch):
    async def run():
        closing = asyncio.Event()
        resume_old = asyncio.Event()
        finalize = LangChainInvestigation._finalize

        async def block_old(runtime, context, **fields):
            if context.scope.lease.generation == 1:
                closing.set()
                await resume_old.wait()
            return await finalize(runtime, context, **fields)

        monkeypatch.setattr(LangChainInvestigation, "_finalize", block_old)
        async with case(tmp_path, responses=[AIMessage(content="只允许提交一次")]) as (driver, sid):
            await enqueue(driver, sid, "只读问题")
            async with driver.claim(sid) as old:
                running = asyncio.create_task(old.drive())
                await asyncio.wait_for(closing.wait(), 5)
                await expire(driver.store, sid)
                async with driver.claim(sid) as new:
                    result = await new.drive()
                    assert result.exit_code == 0
                    resume_old.set()
                    with pytest.raises(LeaseLost):
                        await running
            assert len(driver.model.seen) == 1
            assert (
                len([e for e in await driver.sessions.list_events(sid) if e.type == "result"]) == 1
            )

    asyncio.run(run())


def test_close_drops_in_flight_query_without_another_model_call(tmp_path):
    async def run():
        toolbox = BlockingToolbox()
        async with case(tmp_path, responses=[deploys()], toolbox=toolbox) as (driver, session_id):
            await enqueue(driver, session_id, "查询中关闭")
            async with driver.claim(session_id) as main:
                running = asyncio.create_task(main.drive())
                await asyncio.wait_for(toolbox.called.wait(), 5)
                async with driver.store.transaction() as conn:
                    closed = await driver.sessions.close_session(
                        conn, session_id, reason="测试关闭"
                    )
                toolbox.reply.set()
                with pytest.raises(LeaseLost):
                    await running
            assert len(driver.model.seen) == 1
            assert not await driver.sessions.list_events(session_id, after_seq=closed.seq)
            turn = (await driver.sessions.list_turns(session_id))[0]
            investigation_id = await driver.store.get_investigation_id_for_turn(turn.id)
            assert not await driver.store.list_observations(investigation_id)

    asyncio.run(run())


def test_budget_stops_after_saved_obs_and_next_turn_gets_fresh_budget(tmp_path):
    async def run():
        async with case(
            tmp_path,
            responses=[deploys(), AIMessage(content="第二轮回答")],
            investigation_budget_model_calls=1,
        ) as (driver, session_id):
            await enqueue(driver, session_id, "先查询，然后因预算停止")
            async with driver.claim(session_id) as main:
                incomplete = await main.drive()
                assert incomplete.exit_code == 4
                assert "已取得 1 条有效观测" in incomplete.result.payload["summary"]
                await enqueue(driver, session_id, "继续")
                next_turn = await main.drive()
                assert next_turn.exit_code == 0
                assert next_turn.result.turn_id != incomplete.result.turn_id

    asyncio.run(run())


def test_unsupported_action_path_rejects_preparation(tmp_path):
    async def run():
        class UnsupportedExecutor(FakeAgentExecutor):
            async def prepare_restart_pod(self, **kwargs):
                pytest.fail("基线执行器不能用于新的会话")

        @asynccontextmanager
        async def executor_factory(*, lease, turn_id):
            yield UnsupportedExecutor(driver.store)

        async with case(
            tmp_path,
            responses=[*_preamble(), _valid_report],
            executor_factory=executor_factory,
        ) as (driver, session_id):
            await enqueue(driver, session_id, "排查异常")
            async with driver.claim(session_id) as main:
                result = await main.drive()
            assert result.exit_code == 0 and result.card is None
            investigation_id = result.result.payload["investigation_id"]
            action = await driver.store.get_tool_request(investigation_id, "call-action")
            assert action.error_code == "proposal_unavailable"

    asyncio.run(run())


def test_takeover_voids_unique_unlinked_draft_without_executing(tmp_path):
    async def run():
        sent = []
        proposals = []

        class OrphanExecutor(CardExecutor):
            async def prepare_restart_pod(self, **kwargs):
                proposal, _, _ = await super().prepare_restart_pod(**kwargs)
                proposals.append(proposal.id)
                raise LeaseLost("模拟动作准备提交后、工具绑定前中断")

            async def abandon(self, proposal_id, *, actor, reason):
                assert proposals == [proposal_id]
                async with self.driver.sessions.run(self.lease, self.turn_id):
                    await self.driver.store.set_proposal_status(
                        proposal_id, ProposalStatus.REJECTED
                    )

        @asynccontextmanager
        async def factory(*, lease, turn_id):
            yield OrphanExecutor(driver, lease, turn_id, [], sent)

        async with case(
            tmp_path,
            responses=_preamble(),
            executor_factory=factory,
            namespace="vigil-shop",
        ) as (driver, sid):
            await enqueue(driver, sid, "排查异常")
            async with driver.claim(sid) as old:
                with pytest.raises(LeaseLost, match="工具绑定前"):
                    await old.drive()
            driver.model.responses[:] = [_valid_report]
            async with driver.claim(sid) as main:
                done = await main.drive()
                assert done.exit_code == 0 and done.card is None
            assert (await driver.store.get_proposal(proposals[0])).status is ProposalStatus.REJECTED
            assert not sent
            assert (await driver.store.get_attempt(proposals[0])) is None
            request = await driver.store.get_tool_request(
                done.result.payload["investigation_id"], "call-action"
            )
            assert request.error_code == "prepare_interrupted"

    asyncio.run(run())


def test_takeover_replays_committed_action_receipt_without_executing_again(tmp_path, monkeypatch):
    async def run():
        sent = []
        finish_request = agent_tools._finish_request
        crashed = False

        async def crash_after_receipt(context, call_id, **fields):
            nonlocal crashed
            if not crashed and fields.get("action_attempt_id") is not None:
                crashed = True
                raise LeaseLost("模拟动作已结束但 Agent 尚未落反馈")
            return await finish_request(context, call_id, **fields)

        @asynccontextmanager
        async def factory(*, lease, turn_id):
            yield CardExecutor(driver, lease, turn_id, [], sent)

        monkeypatch.setattr(agent_tools, "_finish_request", crash_after_receipt)
        async with case(
            tmp_path,
            responses=_preamble(),
            executor_factory=factory,
            namespace="vigil-shop",
        ) as (driver, sid):
            await enqueue(driver, sid, "排查异常")
            async with driver.claim(sid) as main:
                paused = await main.drive()
                proposal_id = paused.card.payload["card_id"]
                await persist_decision(proposal_id)
                with pytest.raises(LeaseLost, match="尚未落反馈"):
                    await main.drive()
            assert crashed and sent == [proposal_id]

            def confirm_receipt(messages):
                feedback = json.loads(messages[-1].content)
                assert feedback["status"] == "replayed"
                assert feedback["proposal_id"] == proposal_id
                return _valid_report(messages)

            driver.model.responses[:] = [confirm_receipt]
            async with driver.claim(sid) as new:
                done = await new.drive()
                assert done.exit_code == 0
            assert sent == [proposal_id]
            saved = await driver.store.get_tool_request(
                done.result.payload["investigation_id"], "call-action"
            )
            assert saved.state is ToolRequestState.ACTION_EXECUTED
            assert saved.action_attempt_id is not None

    asyncio.run(run())


def test_side_started_before_approval_finishes_without_changing_main(tmp_path):
    async def run():
        side_toolbox = BlockingToolbox()
        sent = []

        @asynccontextmanager
        async def factory(*, lease, turn_id):
            yield CardExecutor(driver, lease, turn_id, [], sent)

        async with case(
            tmp_path,
            responses=_preamble(),
            executor_factory=factory,
            namespace="vigil-shop",
        ) as (driver, sid):
            await enqueue(driver, sid, "排查异常")
            async with driver.claim(sid) as main:
                paused = await main.drive()
                await enqueue(driver, sid, "有几个副本？")
                side_driver = ConversationDriver(
                    settings=driver.settings,
                    store=driver.store,
                    toolbox=side_toolbox,
                    model=ScriptedChatModel(responses=[deploys(), AIMessage(content="旁路完成")]),
                )
                async with side_driver.claim(sid, lane="side") as side:
                    running = asyncio.create_task(side.drive())
                    await asyncio.wait_for(side_toolbox.called.wait(), 5)
                    proposal_id = paused.card.payload["card_id"]
                    await persist_decision(proposal_id)
                    driver.model.responses[:] = [_valid_report]
                    done = await main.drive()
                    assert done.exit_code == 0 and sent == [proposal_id]
                    side_toolbox.reply.set()
                    answer = await running
                assert answer.result.payload["summary"] == "旁路完成" and answer.exit_code == 3
                assert answer.result.turn_id != done.result.turn_id
                assert (await driver.sessions.get_session(sid)).state == "idle"

                def later(messages):
                    assert any(
                        isinstance(m, HumanMessage) and "旁路完成" in m.content for m in messages
                    )
                    return AIMessage(content="收到前一轮旁路线索")

                await enqueue(driver, sid, "后续追问")
                driver.model.responses[:] = [later]
                assert (await main.drive()).exit_code == 0
            assert sent == [proposal_id]

    asyncio.run(run())
