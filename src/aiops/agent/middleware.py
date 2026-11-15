"""Agent 中间件：预算、取消、批次约束、无进展检测、结构化报告错误处理。

依赖判断留给模型；ID、引用、权限、幂等与恢复判定交给代码。
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from loguru import logger
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    PermissionDeniedError,
    RateLimitError,
)

from aiops.agent.budget import BudgetExhausted
from aiops.agent.context import AgentContext

RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
MAX_MODEL_ATTEMPTS = 5


class RunCancelled(RuntimeError):
    """任务收到取消请求；在模型或工具安全检查点消费。"""


class NoProgress(RuntimeError):
    """连续多个模型轮次没有新进展。"""


class ModelFailure(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class InvalidReport(RuntimeError):
    """结构化报告两次不符合运行时要求。"""


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    if isinstance(exc, (AuthenticationError, PermissionDeniedError, BadRequestError)):
        return False
    if isinstance(exc, (APITimeoutError, APIConnectionError, RateLimitError)):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code in RETRYABLE_STATUS
    return False


async def cancel_requested(ctx: AgentContext) -> bool:
    task = await ctx.store.get_task(ctx.investigation_id)
    return bool(task and task.cancel_requested_at is not None)


def _batch_tool_calls(state: dict[str, Any]) -> list[dict[str, Any]]:
    messages = state.get("messages") or []
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            return list(message.tool_calls or [])
    return []


class BudgetMiddleware(AgentMiddleware):
    """模型调用计数、重试与硬截止；每次尝试先扣预算。"""

    def __init__(self, *, configured_timeout: float, max_retries: int) -> None:
        super().__init__()
        self._timeout = configured_timeout
        self._max_retries = max(0, max_retries)

    async def abefore_model(self, state, runtime):
        ctx: AgentContext = runtime.context
        if await cancel_requested(ctx):
            raise RunCancelled("任务收到取消请求")
        if ctx.budget.no_progress_rounds >= 3:
            raise NoProgress("连续三个模型轮次没有新进展")
        ctx.budget.check_model_budget()
        return None

    async def awrap_model_call(self, request: ModelRequest, handler):  # noqa: ANN001
        ctx: AgentContext = request.runtime.context
        remaining_tools = ctx.budget.remaining()["tool_calls"]
        if not ctx.progress.budget_nudged and remaining_tools <= 6:
            ctx.progress.budget_nudged = True
            base = request.system_message.content if request.system_message else ""
            request = request.override(
                system_message=SystemMessage(
                    content=(
                        f"{base}\n\n运行时提醒：当前只剩 {remaining_tools} 次工具调用"
                        "（assess_evidence 与 request_action 也计入）。请在本批工具结果返回后"
                        "立即用 assess_evidence 固定关键证据、更新假设，然后提交结构化报告；"
                        "不要再扩大取证范围。"
                    )
                )
            )
        attempts = 0
        last_error: BaseException | None = None
        while attempts <= self._max_retries:
            attempts += 1
            await ctx.budget.spend_model_call()
            timeout = ctx.budget.model_call_timeout(configured=self._timeout)
            try:
                async with asyncio.timeout(timeout):
                    return await handler(request)
            except BudgetExhausted:
                raise
            except Exception as exc:  # noqa: BLE001 - 只对可重试的瞬态错误重试
                if not _retryable(exc):
                    raise ModelFailure(
                        "model_request_failed",
                        f"模型调用失败且不可重试：{exc.__class__.__name__}",
                    ) from exc
                last_error = exc
                logger.warning("模型调用第 {} 次失败：{}", attempts, exc.__class__.__name__)
                if attempts > self._max_retries:
                    break
                # 重试前先扣下一条预算；扣不动就直接结束。
                ctx.budget.check_model_budget()
                await asyncio.sleep(min(8.0, 0.5 * 2 ** (attempts - 1)))
        reason = last_error.__class__.__name__ if last_error else "unknown"
        raise ModelFailure(
            "model_retry_exhausted",
            f"模型调用重试耗尽（{attempts} 次）：{reason}",
        )


class ProgressMiddleware(AgentMiddleware):
    """无进展检测：新观测、假设变更、动作状态变化才算进展。"""

    async def aafter_model(self, state, runtime):
        ctx: AgentContext = runtime.context
        tool_calls = _batch_tool_calls(state)
        if not tool_calls:
            await ctx.budget.bump_no_progress()
            return None
        if ctx.progress.activity == ctx.progress.seen_activity:
            await ctx.budget.bump_no_progress()
        else:
            ctx.progress.seen_activity = ctx.progress.activity
            await ctx.budget.reset_no_progress()
        return None


class ActionBatchMiddleware(AgentMiddleware):
    """一个响应不得混合写动作与其他工具；混用整批不执行。"""

    async def awrap_tool_call(self, request, handler):  # noqa: ANN001
        ctx: AgentContext = request.runtime.context
        if await cancel_requested(ctx):
            raise RunCancelled("任务收到取消请求")
        tool_call = request.tool_call
        batch = _batch_tool_calls(request.state)
        names = [item.get("name") for item in batch]
        if "request_action" in names:
            if len(batch) > 1:
                return ToolMessage(
                    content=(
                        "动作请求必须单独出现在一个模型响应里，本次整批都没有执行。"
                        f"本批包含：{', '.join(str(name) for name in names)}。"
                        "请只提交一个 request_action，等反馈后再继续其他工具。"
                    ),
                    name=str(tool_call.get("name")),
                    tool_call_id=str(tool_call.get("id")),
                    status="error",
                )
            if ctx.progress.recovered:
                return ToolMessage(
                    content="业务已恢复，禁止新增动作；只能只读收尾并提交报告。",
                    name="request_action",
                    tool_call_id=str(tool_call.get("id")),
                    status="error",
                )
        # 串行执行：同批工具按批内顺序进入，避免并行取证改变观测顺序。
        async with ctx.progress.tool_lock:
            return await handler(request)


class ReportErrorGuard:
    """结构化报告错误只纠正一次；再次失败即终止。"""

    def __init__(self) -> None:
        self.errors = 0

    def handle(self, exc: Exception) -> str:
        self.errors += 1
        if self.errors > 1:
            raise InvalidReport(f"结构化报告两次不符合要求：{exc}") from exc
        return (
            "结构化报告工具的参数不符合 schema。请修正参数后重新调用报告工具，"
            f"错误：{exc.__class__.__name__}"
        )


__all__ = [
    "ActionBatchMiddleware",
    "BudgetMiddleware",
    "InvalidReport",
    "ModelFailure",
    "NoProgress",
    "ProgressMiddleware",
    "ReportErrorGuard",
    "RunCancelled",
    "cancel_requested",
]
