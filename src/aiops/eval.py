"""评估清单：冻结场景与逐次运行记录，按固定规则汇总 MVP 门槛。

清单是人工维护的版本化记录，评分不读取注入真值来选择动作，只核对已登记的事实。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class ScenarioRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    valid: bool
    reason: str
    investigation_id: str = ""
    proposal_id: str = ""
    attempt_id: str = ""
    recovered: bool = False
    investigation_correct: bool = False


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version: int
    card: str
    signal_classes: list[str] = Field(min_length=1)
    allowed_actions: list[str]
    core: bool = True
    runs: list[ScenarioRun]


class BaselineRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: str
    run_id: str
    investigation_id: str = ""
    rules_only: bool = True
    recovered: bool = False


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frozen_at: str
    scenarios: list[Scenario] = Field(min_length=1)
    baseline: list[BaselineRun] = Field(default_factory=list)
    demo_packs: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class CriterionResult:
    id: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class EvaluationReport:
    frozen_at: str
    criteria: list[CriterionResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(item.passed for item in self.criteria)


class StoreProtocol(Protocol):
    async def get_investigation(self, investigation_id: str) -> Any: ...
    async def count_proposals_by_disposition(self, disposition: str) -> int: ...
    async def get_proposal(self, proposal_id: str) -> Any: ...
    async def get_attempt(self, proposal_id: str) -> Any: ...


def load_manifest(path: Path) -> Manifest:
    return Manifest.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _distinct(values: list[str]) -> int:
    return len({value for value in values if value})


async def evaluate(manifest: Manifest, store: StoreProtocol) -> EvaluationReport:
    """逐条核对清单；任一断言缺证据就记失败，不补默认通过。"""
    criteria: list[CriterionResult] = []

    missing_cards = [item.id for item in manifest.scenarios if not item.card.strip()]
    for item in manifest.scenarios:
        path = item.card.split("#", 1)[0]
        if missing_cards or not Path(path).exists():
            if item.id not in missing_cards:
                missing_cards.append(f"{item.id}:{path}")
    core_ids = {item.id for item in manifest.scenarios if item.core}
    missing_core = sorted({"f01", "f02", "f03"} - core_ids)
    thin_signals = [item.id for item in manifest.scenarios if len(set(item.signal_classes)) < 2]
    criteria.append(
        CriterionResult(
            "V01",
            not missing_cards and not thin_signals and not missing_core,
            f"场景 {len(manifest.scenarios)} 个；"
            f"缺卡片 {missing_cards or '无'}；缺核心场景 {missing_core or '无'}；"
            f"信号不足 {thin_signals or '无'}",
        )
    )

    usage: dict[str, int] = {}
    for item in manifest.scenarios:
        for run in item.runs:
            if run.investigation_id:
                usage[run.investigation_id] = usage.get(run.investigation_id, 0) + 1
    thin_runs = []
    no_correct = []
    bad_runs = []
    for item in manifest.scenarios:
        if not item.core:
            continue
        seen: set[str] = set()
        valid = []
        for run in item.runs:
            if run.run_id in seen:
                bad_runs.append(f"{item.id}:{run.run_id}:重复")
                continue
            seen.add(run.run_id)
            if not run.valid:
                continue
            investigation = (
                await store.get_investigation(run.investigation_id)
                if run.investigation_id
                else None
            )
            if investigation is None:
                bad_runs.append(f"{item.id}:{run.run_id}:调查不存在")
                continue
            if usage.get(run.investigation_id, 0) > 1:
                bad_runs.append(f"{item.id}:{run.run_id}:调查被重复使用")
                continue
            valid.append(run)
        if len(valid) < 3:
            thin_runs.append(f"{item.id}={len(valid)}")
        if not any(run.investigation_correct for run in valid):
            no_correct.append(item.id)
    core_count = sum(1 for item in manifest.scenarios if item.core)
    criteria.append(
        CriterionResult(
            "V02",
            core_count > 0 and not thin_runs and not no_correct,
            f"核心场景 {core_count} 个；有效运行不足：{thin_runs or '无'}；"
            f"缺正确调查：{no_correct or '无'}；"
            f"无效登记：{bad_runs or '无'}",
        )
    )

    kinds: dict[str, bool] = {}
    bad_actions = []
    for item in manifest.scenarios:
        for run in item.runs:
            if not (run.valid and run.attempt_id and run.proposal_id):
                continue
            proposal = await store.get_proposal(run.proposal_id)
            attempt = await store.get_attempt(run.proposal_id)
            if proposal is None or attempt is None or attempt.status.value != "verified":
                bad_actions.append(f"{item.id}:{run.run_id}")
                continue
            if proposal.kind.value not in item.allowed_actions:
                bad_actions.append(f"{item.id}:{run.run_id}:动作不在允许集合")
                continue
            if run.recovered:
                kinds[proposal.kind.value] = True
    criteria.append(
        CriterionResult(
            "V03",
            kinds.get("rollout_undo", False) and kinds.get("restart_pod", False),
            f"真实恢复的动作：{sorted(kinds) or '无'}；存疑登记：{bad_actions or '无'}",
        )
    )

    denied_without_attempt = True
    approval_runs = 0
    for item in manifest.scenarios:
        for run in item.runs:
            if not run.proposal_id:
                continue
            proposal = await store.get_proposal(run.proposal_id)
            if proposal is None:
                continue
            if proposal.status.value == "denied":
                denied_without_attempt = denied_without_attempt and not (
                    await store.get_attempt(run.proposal_id)
                )
            attempt = await store.get_attempt(run.proposal_id)
            if run.attempt_id and attempt is not None and attempt.status.value == "verified":
                approval_runs += 1
    auto_count = await store.count_proposals_by_disposition("auto")
    criteria.append(
        CriterionResult(
            "V04",
            auto_count > 0 and denied_without_attempt and approval_runs > 0,
            "自动允许路径 {}；审批后执行 {} 次；被拒绝提案未执行 {}".format(
                f"{auto_count} 条" if auto_count else "未提供",
                approval_runs,
                denied_without_attempt,
            ),
        )
    )

    for criterion in ("V05", "V06", "V07", "V08"):
        criteria.append(
            CriterionResult(
                criterion,
                False,
                "本切片未自动核对，人工核对记录见 demos/ms08/README.md",
            )
        )

    verified_baseline = 0
    for run in manifest.baseline:
        if not run.rules_only:
            continue
        investigation = (
            await store.get_investigation(run.investigation_id)
            if getattr(run, "investigation_id", "")
            else None
        )
        if (
            investigation is not None
            and getattr(investigation, "used_model_calls", None) == 0
            and getattr(getattr(investigation, "status", None), "value", "") == "completed"
        ):
            verified_baseline += 1
    criteria.append(
        CriterionResult(
            "V09",
            verified_baseline > 0 and verified_baseline == len(manifest.baseline),
            f"规则基线运行 {len(manifest.baseline)} 次，可核对 {verified_baseline} 次",
        )
    )
    missing_packs = [
        path
        for path in manifest.demo_packs
        if not (Path(path) / "README.md").exists()
        or not any((Path(path) / name).is_dir() for name in ("screenshots", "outputs"))
    ]
    criteria.append(
        CriterionResult(
            "V10",
            bool(manifest.demo_packs) and not missing_packs,
            f"证据包 {len(manifest.demo_packs)} 个；缺目录 {missing_packs or '无'}",
        )
    )

    return EvaluationReport(frozen_at=manifest.frozen_at, criteria=criteria)
