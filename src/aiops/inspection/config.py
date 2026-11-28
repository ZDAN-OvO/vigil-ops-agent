"""校验本地巡检清单和 SOP 的固定步骤。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from aiops.config import Settings
from aiops.skills import FRONTMATTER, SkillLoadError, SkillSpec, parse_skill

CHECK_IDS = ("workload-readiness", "workload-signal", "node-ready", "entry-probe")
Name = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$", max_length=253)
]
ProbeID = Annotated[str, StringConstraints(min_length=1, max_length=256, strip_whitespace=True)]
SampleAge = Annotated[int, Field(gt=0, le=3600, strict=True)]
MetricName = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z_:][A-Za-z0-9_:]*$", max_length=120)
]
LabelName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]*$", max_length=64)]
LabelValue = Annotated[str, StringConstraints(max_length=256)]
Threshold = Annotated[float, Field(allow_inf_nan=False)]


class InspectionConfigError(ValueError):
    """巡检清单或 SOP 无法安全加载。"""


class Registration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Signal(Registration):
    metric: MetricName
    labels: dict[LabelName, LabelValue] = Field(min_length=1, max_length=12)
    max_age_seconds: SampleAge
    minimum: Threshold | None = None
    maximum: Threshold | None = None

    @model_validator(mode="after")
    def validate_threshold(self) -> Signal:
        if self.minimum is None and self.maximum is None:
            raise ValueError("业务指标至少需要一个阈值")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("业务指标下限大于上限")
        if not any(self.labels.values()):
            raise ValueError("业务指标必须用至少一个非空标签限定查询范围")
        return self


class Workload(Registration):
    namespace: Name
    kind: Literal["Deployment", "StatefulSet"]
    name: Name
    readiness_max_age_seconds: SampleAge
    signal: Signal

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.namespace, self.kind, self.name


class Node(Registration):
    name: Name
    max_age_seconds: SampleAge


class Entry(Registration):
    probe_id: ProbeID
    max_age_seconds: SampleAge


class Manifest(Registration):
    version: Literal[1]
    cluster: Name
    workloads: tuple[Workload, ...] = Field(default=(), max_length=100)
    nodes: tuple[Node, ...] = Field(default=(), max_length=100)
    entries: tuple[Entry, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def validate_scope(self) -> Manifest:
        groups = (
            [item.identity for item in self.workloads],
            [item.name for item in self.nodes],
            [item.probe_id for item in self.entries],
        )
        if not any(groups):
            raise ValueError("巡检清单至少需要一个目标")
        if any(len(group) != len(set(group)) for group in groups):
            raise ValueError("巡检目标重复")
        return self


@dataclass(frozen=True)
class InspectionSOP:
    skill: SkillSpec
    steps: tuple[str, ...]


def load_sop(root: Path, *, expected_hash: str | None = None) -> InspectionSOP:
    try:
        skill = parse_skill(root / "proactive-inspection" / "SKILL.md")
        match = FRONTMATTER.match(skill.content)
        assert match is not None
        steps = yaml.safe_load(match.group(1))["metadata"]["vigil-inspection-steps"]
    except (SkillLoadError, yaml.YAMLError, KeyError, TypeError, RecursionError):
        raise InspectionConfigError("无法读取巡检 SOP 的步骤声明") from None
    if (
        skill.name != "proactive-inspection"
        or not isinstance(steps, list)
        or not all(isinstance(step, str) for step in steps)
        or len(steps) != len(CHECK_IDS)
        or set(steps) != set(CHECK_IDS)
    ):
        raise InspectionConfigError("巡检 SOP 必须各声明一次登记步骤")
    if expected_hash is not None and skill.content_hash != expected_hash:
        raise InspectionConfigError("当前巡检 SOP 与轮次记录的版本不一致")
    return InspectionSOP(skill, tuple(steps))


def load_manifest(settings: Settings) -> Manifest:
    if settings.inspection_config is None:
        raise InspectionConfigError("缺少 VIGIL_INSPECTION_CONFIG")
    try:
        with settings.inspection_config.open("rb") as stream:
            raw = stream.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            raise InspectionConfigError("巡检清单超过 256 KiB")
        manifest = Manifest.model_validate(yaml.safe_load(raw))
    except (OSError, UnicodeError, yaml.YAMLError, RecursionError):
        raise InspectionConfigError("无法读取 YAML 巡检清单") from None
    except ValidationError as exc:
        field = ".".join(map(str, exc.errors()[0]["loc"]))
        raise InspectionConfigError(f"巡检清单字段或目标无效（{field or '清单'}）") from None
    if manifest.cluster != settings.cluster:
        raise InspectionConfigError("巡检清单的集群与 VIGIL_CLUSTER 不一致")
    return manifest


def metric_selector(metric: str, labels: dict[str, str]) -> str:
    """只生成指标名和等值匹配，不接受登记中的 PromQL 表达式。"""
    # Go 字符串支持 JSON 的转义，只保留 UTF-8 字符原文。
    selectors = ",".join(
        f"{name}={json.dumps(value, ensure_ascii=False)}" for name, value in sorted(labels.items())
    )
    return f"{metric}{{{selectors}}}"


def validate_timing(settings: Settings) -> tuple[int, int]:
    intervals = []
    for key, value in (
        ("INTERVAL", settings.inspection_interval_seconds),
        ("TIMEOUT", settings.inspection_timeout_seconds),
    ):
        if not re.fullmatch(r"[1-9][0-9]{0,8}", str(value)):
            raise InspectionConfigError(f"VIGIL_INSPECTION_{key}_SECONDS 必须是正整数")
        intervals.append(int(value))
    return intervals[0], intervals[1]
