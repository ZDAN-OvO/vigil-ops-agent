"""只读委派调查：串行执行、共享预算、观测归属与失败计数。"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from loguru import logger

from aiops.agent.budget import BudgetExhausted
from aiops.agent.context import AgentContext
from aiops.agent.middleware import NoProgress, RunCancelled
from aiops.contracts import Delegation, DelegationStatus
from aiops.sessions.models import LeaseLost
from aiops.skills import SkillSpec, detect_loaded_skills, render_signal_block

CURRENT_CONTEXT: ContextVar[AgentContext | None] = ContextVar(
    "vigil_delegation_context", default=None
)
CURRENT_DELEGATION: ContextVar[str | None] = ContextVar("vigil_current_delegation", default=None)


class DelegationFailure(RuntimeError):
    """同一调查的第 2 次委派失败；继续调查已不可靠。"""


class ReadOnlyViolation(RuntimeError):
    """子 Agent 拿到写入工具或动作请求工具。"""


class ReadOnlyGuardMiddleware(AgentMiddleware):
    """子 Agent 最后一道只读闸门：任何非只读工具调用立即终止。"""

    def __init__(self, allowed: set[str]) -> None:
        super().__init__()
        self._allowed = frozenset(allowed)

    async def awrap_tool_call(self, request, handler):  # noqa: ANN001
        name = str((request.tool_call or {}).get("name") or "")
        if name not in self._allowed:
            raise ReadOnlyViolation(f"子 Agent 调用了非只读工具：{name or 'unknown'}")
        await request.runtime.context.ensure_active()
        return await handler(request)


def _last_human_text(messages: Sequence[BaseMessage]) -> str:
    for message in reversed(list(messages)):
        if isinstance(message, AIMessage):
            continue
        content = getattr(message, "content", "")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return "委派调查"


def _last_ai_text(messages: Sequence[BaseMessage]) -> str:
    for message in reversed(list(messages)):
        if isinstance(message, AIMessage):
            content = message.content
            if isinstance(content, str) and content.strip():
                return content.strip()
            if isinstance(content, list):
                text = "\n".join(
                    str(block.get("text", "")) if isinstance(block, dict) else str(block)
                    for block in content
                ).strip()
                if text:
                    return text
    return ""


def build_task_runnable(
    app: Any,
    *,
    catalog: Sequence[SkillSpec],
    pinned: Sequence[SkillSpec] = (),
    recursion_limit: int = 100,
):
    """把子 Agent 图包装成 deepagents 可直接调用的 Runnable。

    SubAgentMiddleware 不向子图传 runtime context，因此这里用 ContextVar 取主任务
    上下文，保证预算、权限与 Store 是同一份。
    """

    async def _run(state: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        ctx = CURRENT_CONTEXT.get()
        if ctx is None:
            raise RuntimeError("委派调查缺少主任务上下文")
        messages = list(state.get("messages") or [])
        objective = _last_human_text(messages)
        if pinned:
            messages.append(HumanMessage(content=render_signal_block(pinned).strip()))
        delegation = Delegation(
            id=f"dlg-{uuid.uuid4().hex[:12]}",
            investigation_id=ctx.investigation_id,
            objective=objective[:1000],
            status=DelegationStatus.RUNNING,
            started_at=datetime.now(UTC),
        )
        await ctx.store.create_delegation(delegation)
        async with ctx.progress.delegation_lock:
            token = CURRENT_DELEGATION.set(delegation.id)
            try:
                result = await app.ainvoke(
                    {"messages": messages},
                    {**config, "recursion_limit": recursion_limit},
                    context=ctx,
                )
            except (BudgetExhausted, RunCancelled, NoProgress, ReadOnlyViolation) as exc:
                await ctx.store.finish_delegation(
                    delegation.id,
                    status=DelegationStatus.FAILED,
                    result_summary=f"{exc.__class__.__name__}: {exc}"[:2000],
                    skills=[item.name for item in pinned],
                    finished_at=datetime.now(UTC),
                )
                # 共享预算与只读边界的终止信号必须传回主循环，不能降级成普通失败。
                raise
            except (LeaseLost, asyncio.CancelledError):
                raise
            except Exception as exc:  # noqa: BLE001 - 失败要落库并计数
                logger.warning("委派调查失败：{}", exc.__class__.__name__)
                await ctx.store.finish_delegation(
                    delegation.id,
                    status=DelegationStatus.FAILED,
                    result_summary=f"{exc.__class__.__name__}: {exc}"[:2000],
                    skills=[item.name for item in pinned],
                    finished_at=datetime.now(UTC),
                )
                failed = await ctx.store.count_failed_delegations(ctx.investigation_id)
                if failed >= 2:
                    raise DelegationFailure(f"同一调查已有 {failed} 次委派失败") from exc
                return {
                    "messages": [
                        AIMessage(
                            content=(
                                f"委派调查失败（{exc.__class__.__name__}）。"
                                "已记录失败；请继续自行调查，不要重复同样的委派。"
                            )
                        )
                    ]
                }
            finally:
                CURRENT_DELEGATION.reset(token)
        result_messages = list(result.get("messages") or [])
        summary = _last_ai_text(result_messages)[:2000]
        detected = detect_loaded_skills(result_messages, catalog)
        used_skills = list(
            dict.fromkeys([item.name for item in pinned] + [item.name for item in detected])
        )
        await ctx.store.finish_delegation(
            delegation.id,
            status=DelegationStatus.SUCCEEDED,
            result_summary=summary,
            skills=used_skills,
            finished_at=datetime.now(UTC),
        )
        return result

    return RunnableLambda(_run)


__all__ = [
    "CURRENT_CONTEXT",
    "DelegationFailure",
    "ReadOnlyViolation",
    "build_task_runnable",
]
