"""调查交付结果：诊断状态、业务结果、动作、预算与人工待办分开呈现。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aiops.contracts import InvestigationStatus, TaskOutcome, TerminationCode


@dataclass(frozen=True)
class Report:
    """调查交付结果；诊断状态、业务结果、动作与预算分开呈现。"""

    investigation_id: str
    status: InvestigationStatus
    conclusion: str | None
    termination_reason: str | None
    hypotheses: list[dict[str, Any]]
    evidence_count: int
    tool_call_count: int
    proposal_id: str | None = None
    runbook_uses: list[dict[str, Any]] = field(default_factory=list)
    task: dict[str, Any] | None = None
    action_ids: list[str] = field(default_factory=list)
    relations: list[dict[str, Any]] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    remaining_budget: dict[str, Any] = field(default_factory=dict)
    needs_human: list[str] = field(default_factory=list)
    prediction: dict[str, Any] | None = None
    model_summary_completed: bool = True

    @property
    def recovered(self) -> bool:
        return bool(self.task and self.task.get("recovered_at"))

    def exit_code(self) -> int:
        """0 完成有效调查/恢复，3 待审批或需人工，4 策略拒绝，5 信息不足，1 运行故障。"""
        task = self.task or {}
        if task.get("phase") == "waiting_approval":
            return 3
        if task.get("termination_code") == TerminationCode.POLICY_DENIED.value:
            return 4
        outcome = task.get("outcome")
        if outcome in {TaskOutcome.RECOVERED.value, TaskOutcome.DIAGNOSIS_ONLY.value}:
            return 0
        if outcome == TaskOutcome.NEEDS_HUMAN.value:
            return 3
        if outcome == TaskOutcome.INCONCLUSIVE.value:
            return 5
        return 1
