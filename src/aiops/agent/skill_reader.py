"""把框架提供的 Skill 只读文件工具接入调查的请求边界。"""

from __future__ import annotations

import asyncio
import json

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from aiops.agent.middleware import RunCancelled, cancel_requested
from aiops.agent.tools import _finish_request, _prepare_request
from aiops.contracts import ToolRequestState


class SkillReadMiddleware(AgentMiddleware):
    async def awrap_tool_call(self, request, handler):  # noqa: ANN001
        if request.tool_call.get("name") != "read_file":
            return await handler(request)
        ctx = request.runtime.context
        # 主图调用 task 时持有业务工具锁；子图使用独立的文件锁，避免相互等待。
        async with ctx.progress.skill_read_lock:
            if await cancel_requested(ctx):
                raise RunCancelled("任务收到取消请求")
            ctx, call_id, cached = await _prepare_request(
                request.runtime, "read_file", request.tool_call.get("args") or {}
            )
            if cached is not None:
                try:
                    payload = json.loads(cached)
                    if isinstance(payload, dict) and payload.get("type") == "tool":
                        return ToolMessage.model_validate(payload)
                except ValueError:
                    pass
                return ToolMessage(
                    content=cached, name="read_file", tool_call_id=call_id, status="error"
                )
            try:
                async with asyncio.timeout(
                    ctx.budget.read_tool_timeout(configured=ctx.tool_timeout_seconds)
                ):
                    result = await handler(request)
                if not isinstance(result, ToolMessage):
                    raise TypeError("Skill 文件读取未返回工具消息")
            except Exception as exc:  # noqa: BLE001 - 保存失败结果，重放时不重新打开文件
                result = ToolMessage(
                    content=f"Skill 文件读取失败：{exc.__class__.__name__}",
                    name="read_file",
                    tool_call_id=call_id,
                    status="error",
                )
            await _finish_request(
                ctx,
                call_id,
                state=(
                    ToolRequestState.FAILED
                    if result.status == "error"
                    else ToolRequestState.OBSERVED
                ),
                message=result.model_dump_json(),
                error_code="skill_read_failed" if result.status == "error" else None,
            )
            return result
