"""仓库内置 Skill：解析、信号匹配、提示注入与使用记录。

Agent Skills 格式只规定 `name` 与 `description`；信号触发通过 frontmatter 的
`metadata.vigil-auto-load-sources` 和 `metadata.vigil-auto-load-alertnames` 声明。
描述触发交给模型判断，信号触发由程序在调查开始时执行。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from aiops.contracts import Incident, SkillTrigger, SkillUsage
from aiops.sessions.projection import project_payload

FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
MAX_SKILL_BYTES = 1024 * 1024


class SkillLoadError(RuntimeError):
    """Skill 目录缺失、frontmatter 非法或文件不可读。"""


@dataclass(frozen=True)
class SkillSpec:
    name: str
    description: str
    path: Path
    content: str
    content_hash: str
    sources: frozenset[str]
    alert_names: frozenset[str]

    def usage(self, trigger: SkillTrigger) -> SkillUsage:
        return SkillUsage(name=self.name, content_hash=self.content_hash, trigger=trigger)

    def public_event(
        self,
        trigger: SkillTrigger,
        *,
        status: str = "loaded",
        version_known: bool = True,
    ) -> dict[str, Any]:
        payload = {
            "name": self.name,
            "description": self.description,
            "trigger": trigger.value,
            "status": status,
        }
        if version_known:
            payload["content_hash"] = self.content_hash
        return project_payload(payload)


def _split_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item for item in value.replace(",", " ").split() if item]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def parse_skill(path: Path) -> SkillSpec:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SkillLoadError(f"无法读取 Skill：{path}") from exc
    if len(raw) > MAX_SKILL_BYTES:
        raise SkillLoadError(f"Skill 超过 {MAX_SKILL_BYTES} 字节：{path}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillLoadError(f"Skill 不是 UTF-8 文本：{path}") from exc
    match = FRONTMATTER.match(text)
    if match is None:
        raise SkillLoadError(f"Skill 缺少 YAML frontmatter：{path}")
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise SkillLoadError(f"Skill frontmatter 不是合法 YAML：{path}") from exc
    if not isinstance(meta, dict):
        raise SkillLoadError(f"Skill frontmatter 必须是映射：{path}")
    name = str(meta.get("name") or "").strip()
    description = str(meta.get("description") or "").strip()
    if not name or not description:
        raise SkillLoadError(f"Skill 必须声明 name 与 description：{path}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name):
        raise SkillLoadError(f"Skill 名称必须是小写字母、数字和连字符：{name}")
    metadata = meta.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise SkillLoadError(f"Skill metadata 必须是映射：{path}")
    return SkillSpec(
        name=name,
        description=description,
        path=path,
        content=text,
        content_hash=hashlib.sha256(raw).hexdigest(),
        sources=frozenset(_split_list(metadata.get("vigil-auto-load-sources"))),
        alert_names=frozenset(_split_list(metadata.get("vigil-auto-load-alertnames"))),
    )


def load_skill_catalog(root: Path) -> list[SkillSpec]:
    """读取 `skills/<name>/SKILL.md`；目录不存在视为配置错误。"""
    if not root.is_dir():
        raise SkillLoadError(f"Skill 目录不存在：{root}")
    specs: list[SkillSpec] = []
    seen: set[str] = set()
    for path in sorted(root.glob("*/SKILL.md")):
        spec = parse_skill(path)
        if spec.name in seen:
            raise SkillLoadError(f"Skill 名称重复：{spec.name}")
        seen.add(spec.name)
        specs.append(spec)
    if not specs:
        raise SkillLoadError(f"Skill 目录中没有可用的 SKILL.md：{root}")
    return specs


def match_signal_skills(catalog: Sequence[SkillSpec], incident: Incident) -> list[SkillSpec]:
    """按事件来源与 alertname 选出必须在调查开始时直接加载的 Skill。"""
    alert_names = {item.strip() for item in incident.alert_names if item.strip()}
    matched: list[SkillSpec] = []
    for spec in catalog:
        if incident.trigger not in spec.sources:
            continue
        if spec.alert_names and not (spec.alert_names & alert_names):
            continue
        matched.append(spec)
    return matched


def render_signal_block(specs: Sequence[SkillSpec]) -> str:
    """把信号触发的 Skill 全文拼进首条消息，模型不需要再主动读取。"""
    if not specs:
        return ""
    parts = [
        "\n\n## 已按告警信号自动加载的 Skill\n",
        "以下内容由运行时直接加载，请按其中的调查方法执行；不要声称它们来自工具观测。\n",
    ]
    for spec in specs:
        parts.append(f"\n### Skill: {spec.name}\n\n{spec.content.strip()}\n")
    return "".join(parts)


def detect_loaded_skills(messages: Iterable[Any], catalog: Sequence[SkillSpec]) -> list[SkillSpec]:
    """从消息流里识别被模型实际读取过的 Skill。

    SkillsMiddleware 以文件读取披露全文；这里用 frontmatter 起首的特征串判断，
    避免依赖私有字段。信号触发的 Skill 由调用方单独记录。
    """
    texts = []
    for message in messages:
        content = getattr(message, "content", "")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts.append(
                "\n".join(
                    str(block.get("text", "")) if isinstance(block, dict) else str(block)
                    for block in content
                )
            )
    joined = "\n".join(texts)
    return [spec for spec in catalog if f"name: {spec.name}" in joined and "description:" in joined]


def skill_usage_payload(skills: Sequence[SkillUsage]) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in skills]
