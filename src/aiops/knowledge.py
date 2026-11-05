"""结构化 runbook：条目内容是可核实的参考，不是证据，也不构成授权。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Runbook:
    id: str
    version: int
    title: str
    summary: str
    applies_to: tuple[str, ...]
    symptoms: tuple[str, ...]
    steps: tuple[str, ...]
    source: str

    def as_list_item(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "title": self.title,
            "summary": self.summary,
            "applies_to": list(self.applies_to),
            "symptoms": list(self.symptoms),
        }

    def as_detail(self) -> dict[str, Any]:
        return {**self.as_list_item(), "steps": list(self.steps), "source": self.source}


def _text(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"字段 {key} 必须是非空字符串")
    return value


def _text_list(item: dict[str, Any], key: str) -> tuple[str, ...]:
    value = item.get(key, [])
    if not isinstance(value, list) or any(
        not isinstance(entry, str) or not entry.strip() for entry in value
    ):
        raise ValueError(f"字段 {key} 必须是字符串数组")
    return tuple(value)


def load_runbooks(path: Path) -> list[Runbook]:
    """读取 JSON 条目；数组或单个对象都接受，字段类型不符即报错。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload if isinstance(payload, list) else [payload]
    runbooks = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("每条 runbook 必须是对象")
        version = item.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError("字段 version 必须是大于 0 的整数")
        steps = _text_list(item, "steps")
        if not steps:
            raise ValueError("字段 steps 不能为空")
        runbooks.append(
            Runbook(
                id=_text(item, "id"),
                version=version,
                title=_text(item, "title"),
                summary=_text(item, "summary"),
                applies_to=_text_list(item, "applies_to"),
                symptoms=_text_list(item, "symptoms"),
                steps=steps,
                source=_text(item, "source"),
            )
        )
    return runbooks
