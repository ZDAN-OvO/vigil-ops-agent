"""动作风险规则：先硬约束，再决定自动、审批或拒绝。"""

from aiops.contracts import ActionKind, ActionProposal, RiskDecision, RiskDisposition

POLICY_VERSION = "mvp-policy-1"
ALLOWED_NAMESPACES = frozenset({"vigil-shop"})
ALLOWED_KINDS = frozenset({ActionKind.ROLLOUT_UNDO})


def decide(proposal: ActionProposal, *, target_busy: bool) -> RiskDecision:
    """只做规则判断，不接受模型自评分。任何硬约束不满足都直接拒绝。"""
    reasons: list[str] = []
    if proposal.target.namespace not in ALLOWED_NAMESPACES:
        reasons.append(f"命名空间不在白名单：{proposal.target.namespace}")
    if proposal.kind not in ALLOWED_KINDS:
        reasons.append(f"动作未注册：{proposal.kind.value}")
    if target_busy:
        reasons.append("目标已有未完成的执行")

    if reasons:
        return RiskDecision(
            proposal_id=proposal.id,
            disposition=RiskDisposition.DENY,
            policy_version=POLICY_VERSION,
            reasons=reasons,
            requires_compensation=False,
        )

    reasons = [
        f"动作 {proposal.kind.value}",
        "目标在实验命名空间",
        "首次执行需人工审批",
    ]
    if proposal.origin.value == "operator":
        reasons.append("人工发起")
    return RiskDecision(
        proposal_id=proposal.id,
        disposition=RiskDisposition.APPROVAL,
        policy_version=POLICY_VERSION,
        reasons=reasons,
        requires_compensation=False,
    )


def target_key(cluster: str, namespace: str, kind: str, name: str) -> str:
    return f"{cluster}/{namespace}/{kind}/{name}"
