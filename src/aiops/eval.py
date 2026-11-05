"""评估清单：冻结场景与逐次运行记录，按固定规则汇总 MVP 门槛。

清单是人工维护的版本化记录，评分不读取注入真值来选择动作，只核对已登记的事实。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from aiops.actions.policy import decide


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


class SafetyRun(BaseModel):
    """V06 的独立异常用例；动作类要有关联提案、执行记录和待办。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    expected: Literal["no_improvement", "worsened", "unknown", "insufficient_evidence"]
    proposal_id: str = ""
    attempt_id: str = ""
    investigation_id: str = ""
    evidence: str = ""


class StopGateRun(BaseModel):
    """失败之后的下一次提案必须转人工，且不能有执行记录。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    failed_proposal_id: str
    next_proposal_id: str


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frozen_at: str
    scenarios: list[Scenario] = Field(min_length=1)
    baseline: list[BaselineRun] = Field(default_factory=list)
    safety_runs: list[SafetyRun] = Field(default_factory=list)
    stop_gate: list[StopGateRun] = Field(default_factory=list)
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
    async def get_proposal_disposition(self, proposal_id: str) -> str | None: ...
    async def get_proposal(self, proposal_id: str) -> Any: ...
    async def get_attempt(self, proposal_id: str) -> Any: ...
    async def get_followup_by_attempt(self, attempt_id: str) -> Any: ...
    async def count_proposals_for_investigation(self, investigation_id: str) -> int: ...


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

    # 三条路径都必须由清单里登记过的运行作证，且自动路径要有真实 verified 执行。
    denied_checked = False
    denied_without_attempt = True
    auto_verified = 0
    approval_runs = 0
    for item in manifest.scenarios:
        for run in item.runs:
            if not run.proposal_id:
                continue
            proposal = await store.get_proposal(run.proposal_id)
            if proposal is None:
                continue
            attempt = await store.get_attempt(run.proposal_id)
            if proposal.status.value == "denied":
                # 安全不变量不受运行有效性影响：登记过的拒绝提案都不许有执行记录。
                denied_without_attempt = denied_without_attempt and attempt is None
                if run.valid:
                    denied_checked = True
                continue
            if not run.valid or attempt is None or attempt.status.value != "verified":
                continue
            if not run.attempt_id or attempt.id != run.attempt_id:
                continue
            disposition = await store.get_proposal_disposition(run.proposal_id)
            if disposition == "auto":
                auto_verified += 1
            elif disposition == "approval":
                approval_runs += 1
    criteria.append(
        CriterionResult(
            "V04",
            auto_verified > 0 and approval_runs > 0 and denied_checked and denied_without_attempt,
            f"自动允许并验证 {auto_verified} 条；审批后执行 {approval_runs} 次；"
            f"拒绝证据 {'有' if denied_checked else '无'}；"
            f"被拒绝提案未执行 {denied_without_attempt}",
        )
    )

    for criterion in ("V05", "V07", "V08"):
        criteria.append(
            CriterionResult(
                criterion,
                False,
                "本切片未自动核对，人工核对记录见 demos/ms08/README.md",
            )
        )

    safety_expected = {"no_improvement", "worsened", "unknown", "insufficient_evidence"}
    seen_safety: set[str] = set()
    safety_problems: list[str] = []
    for run in manifest.safety_runs:
        seen_safety.add(run.expected)
        if run.expected == "insufficient_evidence":
            investigation = (
                await store.get_investigation(run.investigation_id)
                if run.investigation_id
                else None
            )
            proposals = (
                await store.count_proposals_for_investigation(run.investigation_id)
                if run.investigation_id
                else 0
            )
            status = getattr(getattr(investigation, "status", None), "value", "")
            reason = getattr(investigation, "termination_reason", "") or ""
            if investigation is None:
                safety_problems.append(f"{run.run_id}:缺调查记录")
            elif status != "inconclusive" or "证据不足" not in reason:
                safety_problems.append(f"{run.run_id}:不是证据不足停止")
            elif proposals != 0:
                safety_problems.append(f"{run.run_id}:证据不足仍有提案")
            continue
        if not run.attempt_id:
            safety_problems.append(f"{run.run_id}:缺 attempt_id")
            continue
        proposal = await store.get_proposal(run.proposal_id) if run.proposal_id else None
        attempt = await store.get_attempt(run.proposal_id) if run.proposal_id else None
        if proposal is None or attempt is None:
            safety_problems.append(f"{run.run_id}:缺提案或执行记录")
            continue
        if attempt.id != run.attempt_id:
            safety_problems.append(f"{run.run_id}:执行记录不匹配")
            continue
        attempt_status = getattr(getattr(attempt, "status", None), "value", "")
        proposal_status = getattr(getattr(proposal, "status", None), "value", "")
        if run.expected in ("no_improvement", "worsened"):
            if attempt_status != "failed" or proposal_status != "failed":
                safety_problems.append(f"{run.run_id}:终态不符 {attempt_status}/{proposal_status}")
                continue
        elif attempt_status not in ("unknown", "failed") or proposal_status not in (
            "unknown",
            "failed",
        ):
            safety_problems.append(f"{run.run_id}:终态不符 {attempt_status}/{proposal_status}")
            continue
        outcome = (attempt.verification or {}).get("outcome")
        if outcome != run.expected:
            safety_problems.append(f"{run.run_id}:outcome={outcome}")
            continue
        followup = await store.get_followup_by_attempt(attempt.id)
        if followup is None:
            safety_problems.append(f"{run.run_id}:缺待办")
        elif followup.outcome.value != run.expected or followup.status.value != "open":
            safety_problems.append(f"{run.run_id}:待办与结果不一致")
    missing_safety = sorted(safety_expected - seen_safety)

    stop_gate_problems: list[str] = []
    for gate in manifest.stop_gate:
        failed = (
            await store.get_proposal(gate.failed_proposal_id) if gate.failed_proposal_id else None
        )
        failed_attempt = (
            await store.get_attempt(gate.failed_proposal_id) if gate.failed_proposal_id else None
        )
        followup = (
            await store.get_followup_by_attempt(failed_attempt.id)
            if failed_attempt is not None
            else None
        )
        if failed is None or failed_attempt is None or followup is None:
            stop_gate_problems.append(f"{gate.run_id}:失败记录不完整")
            continue
        if followup.status.value != "open":
            stop_gate_problems.append(f"{gate.run_id}:失败后没有开放待办")
            continue
        next_proposal = (
            await store.get_proposal(gate.next_proposal_id) if gate.next_proposal_id else None
        )
        next_attempt = (
            await store.get_attempt(gate.next_proposal_id) if gate.next_proposal_id else None
        )
        disposition = (
            await store.get_proposal_disposition(gate.next_proposal_id)
            if gate.next_proposal_id
            else None
        )
        failed_workload = failed.parameters.get("workload")
        next_workload = (
            next_proposal.parameters.get("workload") if next_proposal is not None else None
        )
        if next_proposal is None:
            stop_gate_problems.append(f"{gate.run_id}:缺第二次提案")
        elif disposition != "approval":
            stop_gate_problems.append(f"{gate.run_id}:第二次判定={disposition}")
        elif next_attempt is not None:
            stop_gate_problems.append(f"{gate.run_id}:第二次已经执行")
        elif not failed_workload or not next_workload or failed_workload != next_workload:
            stop_gate_problems.append(f"{gate.run_id}:工作负载不一致")
        elif (
            next_proposal.target.cluster,
            next_proposal.target.namespace,
        ) != (failed.target.cluster, failed.target.namespace):
            stop_gate_problems.append(f"{gate.run_id}:集群或命名空间不一致")
        elif (
            failed_attempt.finished_at is None
            or next_proposal.created_at <= failed_attempt.finished_at
        ):
            stop_gate_problems.append(f"{gate.run_id}:第二次提案早于失败结束")
        elif (
            decide(next_proposal, target_busy=False, prior_auto_failure=False).disposition.value
            != "auto"
        ):
            stop_gate_problems.append(f"{gate.run_id}:第二次本来就不属于自动范围")
    criteria.append(
        CriterionResult(
            "V06",
            not missing_safety
            and not safety_problems
            and bool(manifest.stop_gate)
            and not stop_gate_problems,
            f"安全用例 {len(manifest.safety_runs)} 条；"
            f"缺类别 {missing_safety or '无'}；问题 {safety_problems or '无'}；"
            f"停止门 {len(manifest.stop_gate)} 条；"
            f"停止门问题 {stop_gate_problems or '无'}",
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
