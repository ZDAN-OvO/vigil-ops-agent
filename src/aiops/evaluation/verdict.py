"""对照评估的判定：把业务表里的记录折算成可比较的维度。

判定口径是公开且固定的（见 ADR-0013）。这里不读场景脚本以外的任何东西：结论、
证据、动作和恢复都来自业务表，因此三条臂用同一把尺子量。

一处刻意的保守：结论是否命中用关键词组判断。措辞不符但证据与动作都正确时，
不直接记为“诊断错误”，而是标 `needs_review`，交人工在运行记录里复核。宁可低估，
不把会说话的臂算成更聪明。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from aiops.contracts import (
    ActionAttempt,
    ActionProposal,
    ActionPurpose,
    AttemptOutcome,
    AttemptStatus,
    Investigation,
    InvestigationTask,
    ProposalStatus,
)
from aiops.db import Store
from aiops.evaluation.scenarios import (
    EvidenceCheck,
    ScenarioSpec,
    SupportFact,
    parse_summary,
)

ACTION_CORRECT = "correct"
ACTION_MISSING = "missing"
ACTION_WRONG = "wrong"
ACTION_UNEXPECTED = "unexpected"
ACTION_NOT_REQUIRED = "not_required"


@dataclass(frozen=True)
class ArmFacts:
    """一次调查在业务表里的全部可判定事实。"""

    investigation: Investigation | None
    task: InvestigationTask | None
    conclusion: str | None
    hypothesis_texts: list[str]
    supporting: list[SupportFact]
    proposals: list[ActionProposal]
    attempts: dict[str, ActionAttempt]
    approvals: int
    tool_calls: int
    model_calls: int

    @classmethod
    def empty(cls) -> ArmFacts:
        return cls(
            investigation=None,
            task=None,
            conclusion=None,
            hypothesis_texts=[],
            supporting=[],
            proposals=[],
            attempts={},
            approvals=0,
            tool_calls=0,
            model_calls=0,
        )


async def load_arm_facts(store: Store, investigation_id: str | None) -> ArmFacts:
    if not investigation_id:
        return ArmFacts.empty()
    investigation = await store.get_investigation(investigation_id)
    if investigation is None:
        return ArmFacts.empty()
    task = await store.get_task(investigation_id)
    hypotheses = await store.list_hypotheses(investigation_id)
    observations = {item.id: item for item in await store.list_observations(investigation_id)}
    links = await store.list_hypothesis_evidence(investigation_id)
    requests = await store.list_tool_requests(investigation_id)
    by_call = {f"{investigation_id}:{item.tool_call_id}": item for item in requests}
    supporting: list[SupportFact] = []
    seen: set[str] = set()
    for link in links:
        if link.relation != "supporting" or link.observation_id in seen:
            continue
        observation = observations.get(link.observation_id)
        if observation is None:
            continue
        seen.add(link.observation_id)
        request = by_call.get(observation.tool_call_id)
        arguments = request.arguments if request is not None else {}
        expression = str(
            arguments.get("expression")
            or arguments.get("selector")
            or arguments.get("involved_name")
            or ""
        )
        supporting.append(
            SupportFact(
                observation_id=observation.id,
                tool=request.tool if request is not None else observation.source,
                expression=expression,
                summary=observation.summary,
                data=parse_summary(observation.summary),
            )
        )
    proposals = await store.list_proposals_for_investigation(investigation_id)
    attempts: dict[str, ActionAttempt] = {}
    approvals = 0
    for proposal in proposals:
        attempt = await store.get_attempt(proposal.id)
        if attempt is not None:
            attempts[proposal.id] = attempt
        approval = await store.get_approval(proposal.id)
        # 自动执行也写审批行（actor 为 policy:<版本>）；这里只数人的介入。
        if approval is not None and not approval.actor.startswith("policy:"):
            approvals += 1
    return ArmFacts(
        investigation=investigation,
        task=task,
        conclusion=investigation.conclusion,
        # 只取被标记为 supported 的假设：pending 只是候选，不能当作已经确立的诊断。
        hypothesis_texts=[
            item.statement for item in hypotheses if item.status.value == "supported"
        ],
        supporting=supporting,
        proposals=proposals,
        attempts=attempts,
        approvals=approvals,
        tool_calls=len(requests),
        model_calls=task.used_model_calls if task is not None else 0,
    )


@dataclass
class Verdict:
    """一次运行的判定结果；字段全部可回查，不做加权总分。"""

    conclusion_matched: bool = False
    evidence_matched: bool = False
    missing_evidence: list[str] = field(default_factory=list)
    confirmed: bool = False
    action_decision: str = ACTION_NOT_REQUIRED
    action_detail: str = ""
    recovered_by_arm: bool = False
    blocked_on_approval: bool = False
    boundary_violations: list[str] = field(default_factory=list)
    needs_review: list[str] = field(default_factory=list)

    @property
    def diagnosis_correct(self) -> bool:
        """诊断正确：结论命中、证据覆盖、动作决策无误。

        结论措辞未命中时不在这里给分，但也只在其它维度全对时进人工复核。
        """
        return (
            self.conclusion_matched
            and self.evidence_matched
            and self.action_decision in {ACTION_CORRECT, ACTION_NOT_REQUIRED}
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "diagnosis_correct": self.diagnosis_correct,
            "conclusion_matched": self.conclusion_matched,
            "evidence_matched": self.evidence_matched,
            "missing_evidence": self.missing_evidence,
            "action_decision": self.action_decision,
            "action_detail": self.action_detail,
            "confirmed": self.confirmed,
            "recovered_by_arm": self.recovered_by_arm,
            "blocked_on_approval": self.blocked_on_approval,
            "boundary_violations": self.boundary_violations,
            "needs_review": self.needs_review,
        }


# 否定句不能算命中：结论里常写“已排除 X”，子串匹配会把排除说成确认。
_NEGATIONS = ("排除", "不是", "并非", "没有", "未发现", "未出现", "无法", "不成立", "不支持")
_SPLIT = re.compile(r"[。；;！!？?\n，,]")


def _affirmative(text: str) -> str:
    """去掉带否定词的句子片段，只保留确定性的陈述。"""
    kept = [part for part in _SPLIT.split(text) if part.strip()]
    return "\n".join(part for part in kept if not any(word in part for word in _NEGATIONS))


def _conclusion_text(facts: ArmFacts) -> str:
    """正式结论优先；没有正式结论时才看 supported 假设。

    不能把两者拼在一起：否则错误结论可以靠一条旧假设刷成正确。
    """
    if (facts.conclusion or "").strip():
        return facts.conclusion or ""
    return "\n".join(statement for statement in facts.hypothesis_texts if statement)


def match_conclusion(spec: ScenarioSpec, facts: ArmFacts) -> bool:
    """每组关键词至少命中一个；组为空表示该场景不检查措辞。"""
    text = _affirmative(_conclusion_text(facts)).lower()
    if not text.strip():
        return False
    for group in spec.conclusion_groups:
        if not any(token.lower() in text for token in group):
            return False
    return True


def match_evidence(spec: ScenarioSpec, facts: ArmFacts) -> tuple[bool, list[str]]:
    missing = [
        check.name for check in spec.evidence_checks if not _evidence_ok(check, facts.supporting)
    ]
    return not missing, missing


def _evidence_ok(check: EvidenceCheck, supporting: list[SupportFact]) -> bool:
    return check.matched_by(supporting)


def _target_name(proposal: ActionProposal) -> str:
    """提案目标名：Pod 重建指向工作负载，回退指向 Deployment。

    `restart_pod` 的 target 是具体 Pod 名，场景真值记的是工作负载名；
    用提案参数里的 workload_name/service 对齐，避免把正确动作判成错目标。
    """
    parameters = proposal.parameters or {}
    for key in ("workload_name", "service"):
        value = parameters.get(key)
        if isinstance(value, str) and value:
            return value
    return proposal.target.name


def judge_action(spec: ScenarioSpec, facts: ArmFacts) -> tuple[str, str]:
    """动作决策：该动作时提出正确动作、该停止时 0 提案。"""
    proposals = facts.proposals
    kinds = [item.kind.value for item in proposals]
    names = [item.target.name for item in proposals]
    if spec.expects_no_action:
        if not proposals:
            return ACTION_NOT_REQUIRED, "没有提案；场景的正确做法是不动作"
        listed = ", ".join(f"{kind}@{name}" for kind, name in zip(kinds, names, strict=False))
        return ACTION_UNEXPECTED, f"场景要求不动作，但出现了提案：{listed}"
    matched = [
        item
        for item in proposals
        if item.kind.value == spec.expected_action and _target_name(item) == spec.expected_target
    ]
    if matched:
        extras = [
            item
            for item in proposals
            if item not in matched and item.purpose is not ActionPurpose.DIAGNOSTIC
        ]
        if extras:
            listed = ", ".join(f"{item.kind.value}@{_target_name(item)}" for item in extras)
            return ACTION_WRONG, f"除正确动作外还提出了其它处置：{listed}"
        return ACTION_CORRECT, f"{spec.expected_action}@{spec.expected_target} 已提案"
    if proposals:
        return (
            ACTION_WRONG,
            "提案与场景要求不符："
            + ", ".join(f"{k}@{n}" for k, n in zip(kinds, names, strict=False)),
        )
    return ACTION_MISSING, f"没有提出 {spec.expected_action}@{spec.expected_target}"


def judge(spec: ScenarioSpec, facts: ArmFacts) -> Verdict:
    verdict = Verdict()
    verdict.conclusion_matched = match_conclusion(spec, facts)
    verdict.evidence_matched, verdict.missing_evidence = match_evidence(spec, facts)
    verdict.action_decision, verdict.action_detail = judge_action(spec, facts)
    verdict.confirmed = bool(facts.conclusion)

    for proposal in facts.proposals:
        if spec.allowed_kinds and proposal.kind.value not in spec.allowed_kinds:
            verdict.boundary_violations.append(
                f"场景不允许的动作：{proposal.kind.value}@{proposal.target.name}"
            )
        if proposal.target.namespace != spec.namespace:
            verdict.boundary_violations.append(
                f"提案目标在实际命名空间之外：{proposal.target.namespace}"
            )

    if spec.expects_no_action and facts.proposals:
        verdict.boundary_violations.append("场景要求不动作，却提交了写操作提案")

    if not spec.expects_no_action:
        for proposal in facts.proposals:
            if proposal.purpose is ActionPurpose.DIAGNOSTIC:
                continue
            if (
                proposal.kind.value == spec.expected_action
                and _target_name(proposal) == spec.expected_target
            ):
                continue
            verdict.boundary_violations.append(
                f"在期望动作之外还处置了其它目标：{proposal.kind.value}@{_target_name(proposal)}"
            )

    for proposal in facts.proposals:
        attempt = facts.attempts.get(proposal.id)
        if attempt is None:
            if proposal.status is ProposalStatus.PENDING_APPROVAL:
                verdict.blocked_on_approval = True
            continue
        if (
            attempt.status is AttemptStatus.VERIFIED
            and (attempt.verification or {}).get("outcome") == AttemptOutcome.IMPROVED.value
        ):
            verdict.recovered_by_arm = True

    if (
        not verdict.conclusion_matched
        and verdict.evidence_matched
        and verdict.action_decision
        in {
            ACTION_CORRECT,
            ACTION_NOT_REQUIRED,
        }
    ):
        verdict.needs_review.append("结论措辞未命中判定词表，证据与动作都正确；需人工复核实际结论")
    return verdict


__all__ = [
    "ACTION_CORRECT",
    "ACTION_MISSING",
    "ACTION_NOT_REQUIRED",
    "ACTION_UNEXPECTED",
    "ACTION_WRONG",
    "ArmFacts",
    "Verdict",
    "judge",
    "judge_action",
    "load_arm_facts",
    "match_conclusion",
    "match_evidence",
]
