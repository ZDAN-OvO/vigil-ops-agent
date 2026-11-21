"""把框架提供的 Skill 只读文件工具接入调查的请求边界。"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from dataclasses import replace

from deepagents.backends.utils import validate_path
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from aiops.agent.delegation import CURRENT_DELEGATION
from aiops.agent.middleware import RunCancelled, cancel_requested
from aiops.agent.tools import _finish_request, _prepare_request
from aiops.contracts import SkillTrigger, ToolRequestState
from aiops.sessions.models import LeaseLost
from aiops.skills import SkillSpec

_READ_WINDOW = re.compile(r"^@@ lines (\d+)-(\d+) of (\d+)(?: \| [^\n]+)? @@\n", re.M)


def _skill_event(spec: SkillSpec, result: ToolMessage) -> dict | None:
    if result.status == "error":
        return spec.public_event(SkillTrigger.DESCRIPTION, status="failed", version_known=False)
    # 文件工具也会把空窗口返回为 success。核对实际返回的正文和 catalog 中的区间。
    text = result.text
    window = _READ_WINDOW.search(text)
    if window is None:
        return None
    start, end, total = map(int, window.groups())
    source = spec.content.splitlines()
    body = text[window.end() :]
    if not 1 <= start <= end <= total or not body.strip():
        return None
    if body != "\n".join(source[start - 1 : end]):
        return None
    return spec.public_event(
        SkillTrigger.DESCRIPTION, version_known=start == 1 and end == total == len(source)
    )


class SkillReadMiddleware(AgentMiddleware):
    def __init__(self, catalog: Sequence[SkillSpec]) -> None:
        super().__init__()
        self._skills = {f"/{spec.path.parent.name}/{spec.path.name}": spec for spec in catalog}

    def _match(self, path: object) -> SkillSpec | None:
        if not isinstance(path, str):
            return None
        try:
            return self._skills.get(validate_path(path))
        except ValueError:
            return None

    async def awrap_tool_call(self, request, handler):  # noqa: ANN001
        if request.tool_call.get("name") != "read_file":
            return await handler(request)
        ctx = request.runtime.context
        # 主图调用 task 时持有业务工具锁；子图使用独立的文件锁，避免相互等待。
        async with ctx.progress.skill_read_lock:
            if await cancel_requested(ctx):
                raise RunCancelled("任务收到取消请求")
            args = request.tool_call.get("args") or {}
            ctx, call_id, cached = await _prepare_request(request.runtime, "read_file", args)
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
                await ctx.ensure_active()
                async with asyncio.timeout(
                    ctx.budget.read_tool_timeout(configured=ctx.tool_timeout_seconds)
                ):
                    result = await handler(request)
                if not isinstance(result, ToolMessage):
                    raise TypeError("Skill 文件读取未返回工具消息")
            except LeaseLost:
                raise
            except Exception as exc:  # noqa: BLE001 - 保存失败结果，重放时不重新打开文件
                result = ToolMessage(
                    content=f"Skill 文件读取失败：{exc.__class__.__name__}",
                    name="read_file",
                    tool_call_id=call_id,
                    status="error",
                )
            spec = self._match(args.get("file_path")) if ctx.scope is not None else None
            public = _skill_event(spec, result) if spec is not None else None
            async with ctx.ledger() as (ledger, tx):
                await _finish_request(
                    replace(ctx, store=ledger, active_transaction=tx),
                    call_id,
                    state=(
                        ToolRequestState.FAILED
                        if result.status == "error"
                        else ToolRequestState.OBSERVED
                    ),
                    message=result.model_dump_json(),
                    error_code="skill_read_failed" if result.status == "error" else None,
                )
                if tx is not None and public is not None:
                    delegated = CURRENT_DELEGATION.get() is not None
                    await tx.append_event(
                        "skill", {**public, "lane": tx.turn.lane, "delegated": delegated}
                    )
                    if "content_hash" in public and not delegated:
                        saved = await ledger.get_investigation(ctx.investigation_id)
                        if saved is not None and all(
                            item.name != spec.name for item in saved.skills
                        ):
                            await ledger.set_investigation_skills(
                                ctx.investigation_id,
                                [*saved.skills, spec.usage(SkillTrigger.DESCRIPTION)],
                            )
            return result
