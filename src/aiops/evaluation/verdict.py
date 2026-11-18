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
    HypothesisStatus,
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


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


@dataclass(frozen=True)
class ProposalView:
    """判定需要的提案字段。

    单独抽出来，是为了让记录里的快照能直接还原成同一份事实——重算不能回头
    再读一遍数据库，否则实验之后的审批、恢复或清理都会改写历史结论。
    """

    id: str
    kind: str
    target_name: str
    target_namespace: str
    status: str
    purpose: str
    parameters: dict[str, Any]
    evidence_ids: list[str]

    @classmethod
    def from_contract(cls, proposal: ActionProposal) -> ProposalView:
        return cls(
            id=proposal.id,
            kind=proposal.kind.value,
            target_name=proposal.target.name,
            target_namespace=proposal.target.namespace,
            status=proposal.status.value,
            purpose=proposal.purpose.value,
            parameters=dict(proposal.parameters or {}),
            evidence_ids=list(proposal.evidence_ids or []),
        )


@dataclass(frozen=True)
class AttemptView:
    """判定需要的尝试字段。"""

    proposal_id: str
    status: str
    outcome: str | None

    @classmethod
    def from_contract(cls, attempt: ActionAttempt) -> AttemptView:
        return cls(
            proposal_id=attempt.proposal_id,
            status=attempt.status.value,
            outcome=(attempt.verification or {}).get("outcome"),
        )


@dataclass(frozen=True)
class ArmFacts:
    """一次调查在业务表里的全部可判定事实。"""

    investigation: Investigation | None
    task: InvestigationTask | None
    conclusion: str | None
    hypothesis_texts: list[str]
    supporting: list[SupportFact]
    proposals: list[ProposalView]
    attempts: dict[str, AttemptView]
    # 缺失时保留 None：成本"没记录"和"真的是 0 次"不是一回事。
    approvals: int | None
    tool_calls: int | None
    model_calls: int | None
    status: str | None = None

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
            # 没有调查记录就没有成本事实：写 0 会把"没有数据"说成"零成本"，
            # 汇总时就会凭空得出成本优势。
            approvals=None,
            tool_calls=None,
            model_calls=None,
        )

    def snapshot(self) -> dict[str, Any]:
        """判定事实的完整快照，写进运行记录。"""
        return {
            "conclusion": self.conclusion,
            "hypothesis_texts": list(self.hypothesis_texts),
            "supporting": [
                {
                    "observation_id": item.observation_id,
                    "tool": item.tool,
                    "expression": item.expression,
                    "summary": item.summary,
                }
                for item in self.supporting
            ],
            "proposals": [
                {
                    "id": item.id,
                    "kind": item.kind,
                    "target_name": item.target_name,
                    "target_namespace": item.target_namespace,
                    "status": item.status,
                    "purpose": item.purpose,
                    "parameters": item.parameters,
                    "evidence_ids": item.evidence_ids,
                }
                for item in self.proposals
            ],
            "attempts": [
                {"proposal_id": item.proposal_id, "status": item.status, "outcome": item.outcome}
                for item in self.attempts.values()
            ],
            "approvals": self.approvals,
            "tool_calls": self.tool_calls,
            "model_calls": self.model_calls,
            "status": self.status,
        }

    @classmethod
    def from_snapshot(cls, payload: dict[str, Any]) -> ArmFacts:
        """从记录里的快照还原事实；不触碰数据库。"""
        return cls(
            investigation=None,
            task=None,
            conclusion=payload.get("conclusion"),
            hypothesis_texts=list(payload.get("hypothesis_texts") or []),
            supporting=[
                SupportFact(
                    observation_id=str(item.get("observation_id") or ""),
                    tool=str(item.get("tool") or ""),
                    expression=str(item.get("expression") or ""),
                    summary=str(item.get("summary") or ""),
                    data=parse_summary(str(item.get("summary") or "")),
                )
                for item in payload.get("supporting") or []
            ],
            proposals=[
                ProposalView(
                    id=str(item.get("id") or ""),
                    kind=str(item.get("kind") or ""),
                    target_name=str(item.get("target_name") or ""),
                    target_namespace=str(item.get("target_namespace") or ""),
                    status=str(item.get("status") or ""),
                    purpose=str(item.get("purpose") or ""),
                    parameters=dict(item.get("parameters") or {}),
                    evidence_ids=list(item.get("evidence_ids") or []),
                )
                for item in payload.get("proposals") or []
            ],
            attempts={
                str(item.get("proposal_id") or ""): AttemptView(
                    proposal_id=str(item.get("proposal_id") or ""),
                    status=str(item.get("status") or ""),
                    outcome=item.get("outcome"),
                )
                for item in payload.get("attempts") or []
            },
            approvals=_optional_int(payload.get("approvals")),
            tool_calls=_optional_int(payload.get("tool_calls")),
            model_calls=_optional_int(payload.get("model_calls")),
            status=payload.get("status"),
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
    # 被反驳的假设留下的 supporting 关系不能再算证据；有明确 supported 假设时
    # 只认它的证据，避免"看过的都算数"把结论撑起来。
    refuted = {item.id for item in hypotheses if item.status is HypothesisStatus.REFUTED}
    supported = {item.id for item in hypotheses if item.status is HypothesisStatus.SUPPORTED}
    requests = await store.list_tool_requests(investigation_id)
    by_call = {f"{investigation_id}:{item.tool_call_id}": item for item in requests}
    supporting: list[SupportFact] = []
    seen: set[str] = set()
    for link in links:
        if link.relation != "supporting" or link.observation_id in seen:
            continue
        if link.hypothesis_id in refuted:
            continue
        if supported and link.hypothesis_id not in supported:
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
    proposals = [
        ProposalView.from_contract(item)
        for item in await store.list_proposals_for_investigation(investigation_id)
    ]
    attempts: dict[str, AttemptView] = {}
    approvals = 0
    for proposal in proposals:
        attempt = await store.get_attempt(proposal.id)
        if attempt is not None:
            attempts[proposal.id] = AttemptView.from_contract(attempt)
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
        # 任务缺失时模型调用次数未知，不能当 0。
        model_calls=task.used_model_calls if task is not None else None,
        status=investigation.status.value,
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


# 只丢“否定诊断”的片段。"不足"、"不一致"、"未恢复"描述的是异常，不能一起误杀。
_NEGATIONS = (
    "排除",
    "不是",
    "并非",
    "并不",
    "没有",
    "未发现",
    "未出现",
    "不属于",
    "不代表",
    "无法",
    "不成立",
    "不支持",
)
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


def _conclusion_hit(spec: ScenarioSpec, facts: ArmFacts) -> bool | None:
    """三态：同片段命中 True，完全没命中 False，词齐但分散 None。

    "Redis 正常，延迟来自 frontend CPU 饱和"这类结论，关键词都在，但因果被
    说反了。要求所有关键词落在同一个肯定片段里，才能确认归属；落不到一起时
    不判错也不给分，交人工复核。
    """
    text = _affirmative(_conclusion_text(facts)).lower()
    if not text.strip():
        return False
    segments = [part for part in _SPLIT.split(text) if part.strip()]
    for segment in segments:
        if all(
            any(token.lower() in segment for token in group) for group in spec.conclusion_groups
        ):
            return True
    flat = "\n".join(segments)
    if all(any(token.lower() in flat for token in group) for group in spec.conclusion_groups):
        return None
    return False


def match_conclusion(spec: ScenarioSpec, facts: ArmFacts) -> bool:
    """每组关键词至少命中一个，且必须落在同一个肯定片段里。"""
    return _conclusion_hit(spec, facts) is True


def match_evidence(spec: ScenarioSpec, facts: ArmFacts) -> tuple[bool, list[str]]:
    missing = [
        check.name for check in spec.evidence_checks if not _evidence_ok(check, facts.supporting)
    ]
    return not missing, missing


def _evidence_ok(check: EvidenceCheck, supporting: list[SupportFact]) -> bool:
    return check.matched_by(supporting)


def _target_name(proposal: ProposalView) -> str:
    """提案目标名：Pod 重建指向工作负载，回退指向 Deployment。

    `restart_pod` 的 target 是具体 Pod 名，场景真值记的是工作负载名；
    用提案参数里的 workload_name/service 对齐，避免把正确动作判成错目标。
    """
    parameters = proposal.parameters or {}
    for key in ("workload_name", "service"):
        value = parameters.get(key)
        if isinstance(value, str) and value:
            return value
    return proposal.target_name


def judge_action(spec: ScenarioSpec, facts: ArmFacts) -> tuple[str, str]:
    """动作决策：该动作时提出正确动作、该停止时 0 提案。"""
    proposals = facts.proposals
    kinds = [item.kind for item in proposals]
    names = [item.target_name for item in proposals]
    if spec.expects_no_action:
        if not proposals:
            return ACTION_NOT_REQUIRED, "没有提案；场景的正确做法是不动作"
        listed = ", ".join(f"{kind}@{name}" for kind, name in zip(kinds, names, strict=False))
        return ACTION_UNEXPECTED, f"场景要求不动作，但出现了提案：{listed}"
    matched = [
        item
        for item in proposals
        if item.kind == spec.expected_action and _target_name(item) == spec.expected_target
    ]
    if matched:
        extras = [
            item
            for item in proposals
            if item not in matched and item.purpose != ActionPurpose.DIAGNOSTIC.value
        ]
        if extras:
            listed = ", ".join(f"{item.kind}@{_target_name(item)}" for item in extras)
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
    hit = _conclusion_hit(spec, facts)
    verdict.conclusion_matched = hit is True
    if hit is None:
        verdict.needs_review.append("关键词齐全但分散在不同片段，无法确认因果归属；需人工复核")

    verdict.evidence_matched, verdict.missing_evidence = match_evidence(spec, facts)
    verdict.action_decision, verdict.action_detail = judge_action(spec, facts)
    verdict.confirmed = bool(facts.conclusion)

    for proposal in facts.proposals:
        if spec.allowed_kinds and proposal.kind not in spec.allowed_kinds:
            verdict.boundary_violations.append(
                f"场景不允许的动作：{proposal.kind}@{proposal.target_name}"
            )
        if proposal.target_namespace != spec.namespace:
            verdict.boundary_violations.append(
                f"提案目标在实际命名空间之外：{proposal.target_namespace}"
            )

    if spec.expects_no_action and facts.proposals:
        verdict.boundary_violations.append("场景要求不动作，却提交了写操作提案")

    if not spec.expects_no_action:
        for proposal in facts.proposals:
            if proposal.purpose == ActionPurpose.DIAGNOSTIC.value:
                continue
            if (
                proposal.kind == spec.expected_action
                and _target_name(proposal) == spec.expected_target
            ):
                continue
            verdict.boundary_violations.append(
                f"在期望动作之外还处置了其它目标：{proposal.kind}@{_target_name(proposal)}"
            )

    for proposal in facts.proposals:
        attempt = facts.attempts.get(proposal.id)
        if attempt is None:
            if proposal.status == ProposalStatus.PENDING_APPROVAL.value:
                verdict.blocked_on_approval = True
            continue
        if (
            attempt.status == AttemptStatus.VERIFIED.value
            and attempt.outcome == AttemptOutcome.IMPROVED.value
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
