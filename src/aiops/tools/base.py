"""只读工具的公共上下文：截止时间、取消信号与原始证据落盘。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RawRef:
    ref: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class ToolContext:
    """一次调查的只读工具边界；调用方负责给出截止时间。"""

    investigation_id: str
    cluster: str
    namespace: str
    runtime_path: Path
    deadline: float
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)

    @classmethod
    def create(
        cls,
        *,
        investigation_id: str,
        cluster: str,
        namespace: str,
        runtime_path: Path,
        timeout_seconds: float,
    ) -> ToolContext:
        return cls(
            investigation_id=investigation_id,
            cluster=cluster,
            namespace=namespace,
            runtime_path=runtime_path,
            deadline=time.monotonic() + timeout_seconds,
        )

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def abort_reason(self) -> str | None:
        """取消优先于超时；两者都表示本次调用不得继续。"""
        if self.cancelled.is_set():
            return "cancelled"
        if self.remaining() <= 0:
            return "timeout"
        return None

    def persist_raw(self, tool_call_id: str, payload: Any) -> RawRef:
        directory = self.runtime_path / "evidence" / self.investigation_id
        directory.mkdir(parents=True, exist_ok=True)
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        path = directory / f"{tool_call_id}.json"
        path.write_bytes(body)
        return RawRef(
            ref=str(path.relative_to(self.runtime_path)),
            sha256=hashlib.sha256(body).hexdigest(),
            bytes=len(body),
        )
