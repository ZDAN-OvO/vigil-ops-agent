"""调查交付结果：诊断状态、业务结果、动作、预算与人工待办分开呈现。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aiops.contracts import (
    Delegation,
    InvestigationStatus,
    Observation,
    TaskOutcome,
    TerminationCode,
)


def delegation_payloads(
    delegations: list[Delegation], observations: list[Observation]
) -> list[dict[str, Any]]:
    """委派记录连同其返回的观测 ID，便于按委派回查观测归属。"""
    return [
        {
            "id": item.id,
            "objective": item.objective,
            "status": item.status.value,
            "result_summary": item.result_summary,
            "skills": item.skills,
            "observation_ids": [
                observation.id
                for observation in observations
                if observation.delegation_id == item.id
            ],
            "started_at": item.started_at.isoformat(),
            "finished_at": item.finished_at.isoformat() if item.finished_at else None,
        }
        for item in delegations
    ]


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
    skills: list[dict[str, Any]] = field(default_factory=list)
    delegations: list[dict[str, Any]] = field(default_factory=list)
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
