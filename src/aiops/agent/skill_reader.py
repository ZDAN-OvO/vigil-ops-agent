"""把框架提供的 Skill 只读文件工具接入调查的请求边界。"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass, replace

from deepagents.backends.filesystem import FilesystemBackend
from deepagents.backends.protocol import ReadResult
from deepagents.backends.utils import file_data_to_string, slice_read_response, validate_path
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from aiops.agent.delegation import CURRENT_DELEGATION
from aiops.agent.middleware import RunCancelled, cancel_requested
from aiops.agent.tools import _finish_request, _prepare_request
from aiops.contracts import SkillTrigger, ToolRequestState
from aiops.sessions.models import LeaseLost
from aiops.skills import MAX_SKILL_BYTES, SkillSpec

_READ_WINDOW = re.compile(r"^@@ lines (\d+)-(\d+) of (\d+)(?: \| [^\n]+)? @@\n", re.M)


@dataclass
class _SkillRead:
    path: str
    content: str | None = None


_skill_read: ContextVar[_SkillRead | None] = ContextVar("skill_read", default=None)


class SkillFilesystemBackend(FilesystemBackend):
    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        capture = _skill_read.get()
        if capture is None or capture.path != file_path:
            return super().read(file_path, offset, limit)

        # 同一次文件打开取得上下文，再用与文件工具相同的规则生成实际行窗。
        full = super().read(file_path, 0, MAX_SKILL_BYTES + 1)
        if full.file_data is None or full.start_line is None:
            return full
        content = file_data_to_string(full.file_data)
        if full.next_offset is None and len(content.encode("utf-8")) <= MAX_SKILL_BYTES:
            capture.content = content
        return slice_read_response(full.file_data, offset, limit)


def _skill_event(
    spec: SkillSpec, result: ToolMessage, verified_content: str | None = None
) -> dict | None:
    if result.status == "error":
        return spec.public_event(SkillTrigger.DESCRIPTION, status="failed", version_known=False)
    # 文件工具也会把空窗口返回为 success。核对返回的正文和同次打开取得的行窗。
    text = result.text
    window = _READ_WINDOW.search(text)
    if window is None:
        return None
    start, end, total = map(int, window.groups())
    body = text[window.end() :]
    if not 1 <= start <= end <= total or not body.strip():
        return None
    lines = verified_content.splitlines() if verified_content is not None else []
    if total != len(lines) or body != "\n".join(lines[start - 1 : end]):
        return spec.public_event(SkillTrigger.DESCRIPTION, version_known=False)
    return spec.public_event(
        SkillTrigger.DESCRIPTION,
        version_known=verified_content == spec.content,
        line_range=(start, end),
        verified_content=verified_content,
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
            spec = self._match(args.get("file_path")) if ctx.scope is not None else None
            capture = _SkillRead(validate_path(args["file_path"])) if spec is not None else None
            token = _skill_read.set(capture)
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
            finally:
                _skill_read.reset(token)
            public = _skill_event(spec, result, capture.content) if capture is not None else None
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
                    # 整份已知版本计入调查用法，分段读取仍保留公开事件。
                    window = _READ_WINDOW.search(result.text)
                    if (
                        "content_hash" in public
                        and window is not None
                        and window[1] == "1"
                        and window[2] == window[3]
                        and not delegated
                    ):
                        saved = await ledger.get_investigation(ctx.investigation_id)
                        if saved is not None and all(
                            item.name != spec.name for item in saved.skills
                        ):
                            await ledger.set_investigation_skills(
                                ctx.investigation_id,
                                [*saved.skills, spec.usage(SkillTrigger.DESCRIPTION)],
                            )
            return result
