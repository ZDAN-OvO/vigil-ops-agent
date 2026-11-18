"""调查交付结果：诊断状态、业务结果、动作、预算与人工待办分开呈现。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from aiops.contracts import (
    AttemptStatus,
    Delegation,
    InvestigationStatus,
    Observation,
    ProposalStatus,
    TaskOutcome,
    TerminationCode,
)

if TYPE_CHECKING:
    from aiops.db import Store


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


async def build_report(
    store: Store,
    investigation_id: str,
    *,
    remaining_budget: dict[str, Any],
    needs_human: list[str],
    calendar_seconds: float,
    model_summary_completed: bool = True,
) -> Report:
    """按已落库的记录组装交付结果；Agent 与规则基线共用这一份口径。

    报告只陈述记录里存在的事实：提案、执行、待办、预算都来自业务表，
    不因某一臂是模型还是规则而改变字段含义。
    """
    saved = await store.get_investigation(investigation_id)
    if saved is None:
        raise LookupError(f"调查不存在：{investigation_id}")
    task = await store.get_task(investigation_id)
    hypotheses = await store.list_hypotheses(investigation_id)
    observations = await store.list_observations(investigation_id)
    relations = await store.list_hypothesis_evidence(investigation_id)
    tools = await store.list_tool_requests(investigation_id)
    delegations = await store.list_delegations(investigation_id)
    proposals = await store.list_proposals_for_investigation(investigation_id)
    latest = proposals[-1] if proposals else None
    prediction = None
    if latest is not None and latest.prediction:
        attempt = await store.get_attempt(latest.id)
        verification = (attempt.verification if attempt else None) or {}
        prediction = {
            "proposal_id": latest.id,
            "purpose": latest.purpose.value,
            "prediction": latest.prediction,
            "observation_conditions": latest.observation_conditions,
            "observation_window_seconds": latest.observation_window_seconds,
            "outcome": verification.get("outcome"),
            "outcome_reason": verification.get("outcome_reason"),
        }
    items = list(needs_human)
    for proposal in proposals:
        attempt = await store.get_attempt(proposal.id)
        if attempt is None:
            continue
        if attempt.status in {AttemptStatus.FAILED, AttemptStatus.UNKNOWN} and (
            proposal.status in {ProposalStatus.FAILED, ProposalStatus.UNKNOWN}
        ):
            items.append(f"{proposal.id}: 执行结果 {(attempt.verification or {}).get('outcome')}")
    task_payload = None
    if task is not None:
        task_payload = {
            "phase": task.phase.value,
            "outcome": task.outcome.value if task.outcome else None,
            "termination_code": task.termination_code,
            "runtime_version": task.runtime_version,
            "proposal_id": task.proposal_id,
            "no_progress_rounds": task.no_progress_rounds,
            "recovered_at": task.recovered_at.isoformat() if task.recovered_at else None,
            "investigation_seconds": round(task.investigation_seconds, 1),
            "action_seconds": round(task.action_seconds, 1),
            "calendar_seconds": round(calendar_seconds, 1),
            "used_model_calls": task.used_model_calls,
            "used_tool_calls": task.used_tool_calls,
        }
    return Report(
        investigation_id=investigation_id,
        status=saved.status,
        conclusion=saved.conclusion,
        termination_reason=saved.termination_reason,
        hypotheses=[
            {
                "id": item.id,
                "parent_id": item.parent_id,
                "statement": item.statement,
                "status": item.status.value,
                "update_reason": item.update_reason,
            }
            for item in hypotheses
        ],
        evidence_count=len(observations),
        tool_call_count=len(tools),
        proposal_id=latest.id if latest else None,
        task=task_payload,
        action_ids=[item.id for item in proposals],
        relations=[
            {
                "hypothesis_id": item.hypothesis_id,
                "observation_id": item.observation_id,
                "relation": item.relation,
                "rationale": item.rationale,
            }
            for item in relations
        ],
        observations=[
            {
                "id": item.id,
                "source": item.source,
                "status": item.status.value,
                "summary": item.summary[:300],
                "raw_ref": item.raw_ref,
            }
            for item in observations
        ],
        skills=[item.model_dump(mode="json") for item in saved.skills],
        delegations=delegation_payloads(delegations, observations),
        remaining_budget=remaining_budget,
        needs_human=items,
        prediction=prediction,
        model_summary_completed=model_summary_completed,
    )
