"""分阶段预算：跨进程累计、重放不重复计数、重试与动作预留。"""

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from db_env import DSN
from investigation_support import agent_case, call, report_call, tool_payloads
from langchain.tools import ToolRuntime
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult
from openai import APITimeoutError

from aiops.agent.budget import BudgetLedger, WriteReserveUnavailable
from aiops.agent.tools import _finish_request, _prepare_request
from aiops.contracts import (
    Budget,
    InvestigationTask,
    TaskOutcome,
    TaskPhase,
    TerminationCode,
    ToolRequestState,
)

pytestmark = pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL，跳过真实库测试")


def _task(investigation_id: str) -> InvestigationTask:
    return InvestigationTask(
        investigation_id=investigation_id,
        phase=TaskPhase.RUNNING,
        runtime_version="langchain-agent-1",
        calendar_started_at=datetime.now(UTC),
    )


def test_phase_seconds_persist_across_restart(ids):
    async def run():
        async with agent_case(ids, responses=[]) as case:
            await case.store.create_task(_task(ids["investigation"]))
            task = await case.store.get_task(ids["investigation"])
            first = BudgetLedger.restore(
                store=case.store,
                investigation_id=ids["investigation"],
                budget=case.investigation.budget,
                task=task,
            )
            await first.start()
            await asyncio.sleep(0.06)
            await first.begin_action()
            await asyncio.sleep(0.03)
            await first.end_action()
            investigation_before_pause = first.investigation_seconds
            action_before_pause = first.action_seconds
            await first.pause()
            restored = await case.store.get_task(ids["investigation"])
            second = BudgetLedger.restore(
                store=case.store,
                investigation_id=ids["investigation"],
                budget=case.investigation.budget,
                task=restored,
            )
            return (
                investigation_before_pause,
                action_before_pause,
                second.investigation_seconds,
                second.action_seconds,
            )

    investigation_seconds, action_seconds, restored_investigation, restored_action = asyncio.run(
        run()
    )
    assert investigation_seconds >= 0.05
    assert action_seconds >= 0.02
    assert restored_investigation == pytest.approx(investigation_seconds, abs=0.005)
    assert restored_action == pytest.approx(action_seconds, abs=0.005)
    assert restored_action < restored_investigation + restored_action


def test_replayed_request_does_not_spend_tool_budget(ids):
    async def run():
        async with agent_case(ids, responses=[]) as case:
            await case.store.create_task(_task(ids["investigation"]))
            _, context = await case.runtime._open_context()
            await context.budget.start()
            fake_runtime = ToolRuntime(
                state={},
                context=context,
                config={},
                stream_writer=None,
                tool_call_id="call-replay",
                store=None,
            )
            _, call_id, cached_first = await _prepare_request(
                fake_runtime, "manage_hypotheses", {"operations": []}
            )
            assert cached_first is None
            await _finish_request(
                context,
                call_id,
                state=ToolRequestState.OBSERVED,
                message="第一次结果",
            )
            _, _, cached_second = await _prepare_request(
                fake_runtime, "manage_hypotheses", {"operations": []}
            )
            await context.budget.pause()
            task = await case.store.get_task(ids["investigation"])
            return cached_second, task.used_tool_calls

    cached, used = asyncio.run(run())
    assert cached == "第一次结果"
    assert used == 1


class FlakyModel(BaseChatModel):
    """前 failures 次调用抛超时，之后返回预置消息。"""

    failures: int
    message: object
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "flaky"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:  # noqa: ANN001
        self.calls += 1
        if self.failures > 0:
            self.failures -= 1
            raise APITimeoutError(request=httpx.Request("POST", "http://model.invalid"))
        return ChatResult(generations=[ChatGeneration(message=self.message)])


def test_model_retry_counts_every_attempt(ids):
    async def run():
        async with agent_case(ids, responses=[]) as case:
            model = FlakyModel(
                failures=2, message=report_call(confirmed=False, conclusion="重试后成功")
            )
            case.runtime._model = model
            report = await case.runtime.run()
            task = await case.store.get_task(ids["investigation"])
            return report, task.used_model_calls, model.calls

    report, used, calls = asyncio.run(run())
    assert calls == 3
    assert used == 3
    assert report.task["outcome"] == TaskOutcome.INCONCLUSIVE.value


def test_model_retry_stops_when_budget_is_exhausted(ids):
    budget = Budget(
        max_tool_calls=5,
        max_model_calls=1,
        max_wall_clock_seconds=120,
        max_action_seconds=120,
    )

    async def run():
        async with agent_case(ids, responses=[], budget=budget) as case:
            model = FlakyModel(
                failures=4, message=report_call(confirmed=False, conclusion="不该到这里")
            )
            case.runtime._model = model
            report = await case.runtime.run()
            return report, model.calls

    report, calls = asyncio.run(run())
    assert calls == 1
    assert report.task["outcome"] == TaskOutcome.ABORTED.value
    assert report.task["termination_code"] == TerminationCode.BUDGET_EXHAUSTED.value


def test_write_reserve_needs_action_and_model_room(ids):
    async def run():
        async with agent_case(ids, responses=[]) as case:
            await case.store.create_task(_task(ids["investigation"]))
            task = await case.store.get_task(ids["investigation"])
            ledger = BudgetLedger.restore(
                store=case.store,
                investigation_id=ids["investigation"],
                budget=case.investigation.budget,
                task=task,
            )
            results = []
            try:
                ledger.check_write_reserve(adapter_bound=500, model_reserve=10)
            except WriteReserveUnavailable as exc:
                results.append(str(exc))
            try:
                ledger.check_write_reserve(adapter_bound=10, model_reserve=1000)
            except WriteReserveUnavailable as exc:
                results.append(str(exc))
            ledger.check_write_reserve(adapter_bound=10, model_reserve=10)
            return results

    problems = asyncio.run(run())
    assert len(problems) == 2
    assert "动作预算不足" in problems[0]
    assert "调查预算不足" in problems[1]


def test_tool_budget_exhaustion_is_reported(ids):
    async def run():
        async with agent_case(
            ids,
            responses=[
                call("kubernetes_list_pods", {"selector": "app=x"}, "call-1"),
                call("kubernetes_list_pods", {"selector": "app=x"}, "call-2"),
                report_call(confirmed=False, conclusion="工具预算耗尽，基于已取观测无法继续取证"),
            ],
            budget=Budget(
                max_tool_calls=1,
                max_model_calls=5,
                max_wall_clock_seconds=120,
                max_action_seconds=120,
            ),
        ) as case:
            report = await case.runtime.run()
            payloads = tool_payloads(case.model.seen[-1])
            return report, payloads

    report, payloads = asyncio.run(run())
    assert report.task["outcome"] == TaskOutcome.INCONCLUSIVE.value
    assert report.task["termination_code"] == TerminationCode.INSUFFICIENT_EVIDENCE.value
    assert report.task["used_tool_calls"] == 1
    assert any(item.get("code") == "budget_exhausted" for item in payloads)


def test_write_reserve_needs_one_model_call_left(ids):
    from aiops.contracts import InvestigationTask, TaskPhase

    async def run():
        async with agent_case(ids, responses=[]) as case:
            task = InvestigationTask(
                investigation_id=ids["investigation"],
                phase=TaskPhase.RUNNING,
                runtime_version="langchain-agent-1",
                calendar_started_at=datetime.now(UTC),
                used_model_calls=case.investigation.budget.max_model_calls,
            )
            await case.store.create_task(task)
            stored = await case.store.get_task(ids["investigation"])
            ledger = BudgetLedger.restore(
                store=case.store,
                investigation_id=ids["investigation"],
                budget=case.investigation.budget,
                task=stored,
            )
            try:
                ledger.check_write_reserve(adapter_bound=0, model_reserve=0)
            except WriteReserveUnavailable as exc:
                return str(exc)
            return ""

    assert "模型调用预算不足" in asyncio.run(run())


def test_low_tool_budget_adds_one_system_reminder(ids):
    from types import SimpleNamespace

    from langchain.agents.middleware import ModelRequest
    from langchain_core.messages import AIMessage, SystemMessage

    from aiops.agent.middleware import BudgetMiddleware

    async def run():
        async with agent_case(ids, responses=[]) as case:
            _, context = await case.runtime._open_context()
            context.budget.used_tool_calls = 4  # 测试预算 10 次工具，剩 6 次
            seen: list[str] = []

            async def handler(request):
                seen.append(request.system_message.content)
                return AIMessage(content="ok")

            middleware = BudgetMiddleware(configured_timeout=10, max_retries=0)
            request = ModelRequest(
                model=case.model,
                messages=[],
                system_message=SystemMessage(content="基础提示"),
                runtime=SimpleNamespace(context=context),
            )
            await middleware.awrap_model_call(request, handler)
            await middleware.awrap_model_call(request, handler)
            return seen

    seen = asyncio.run(run())
    assert len(seen) == 2
    assert "只剩 6 次工具调用" in seen[0]
    assert "assess_evidence" in seen[0]
    assert seen[1] == "基础提示"


def test_budget_nudge_is_injected_once_when_tool_calls_run_low(ids):
    async def run():
        async with agent_case(
            ids,
            responses=[
                call("kubernetes_list_pods", {"selector": "app=x"}, "call-1"),
                report_call(confirmed=False, conclusion="预算提醒后提交报告"),
            ],
            budget=Budget(
                max_tool_calls=6,
                max_model_calls=5,
                max_wall_clock_seconds=120,
                max_action_seconds=120,
            ),
        ) as case:
            report = await case.runtime.run()
            return report, case.model.seen

    report, seen = asyncio.run(run())
    assert report.task["outcome"] == TaskOutcome.INCONCLUSIVE.value
    notices = [
        str(message.content)
        for messages in seen
        for message in messages
        if "运行时提醒" in str(message.content)
    ]
    assert len(notices) == 1
