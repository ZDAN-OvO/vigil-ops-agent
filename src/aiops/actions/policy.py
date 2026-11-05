"""动作风险规则：先硬约束，再决定自动、审批或拒绝。"""

from aiops.contracts import ActionKind, ActionProposal, RiskDecision, RiskDisposition

POLICY_VERSION = "mvp-policy-2"
ALLOWED_NAMESPACES = frozenset({"vigil-shop", "vigil-experiment"})
# 自动执行只在这里开放：命名空间里只有显式标记为可丢弃的实验负载。
AUTO_NAMESPACES = frozenset({"vigil-experiment"})
ALLOWED_KINDS = frozenset({ActionKind.ROLLOUT_UNDO, ActionKind.RESTART_POD})


def _restart_problems(proposal: ActionProposal) -> list[str]:
    """restart_pod 只能用于有控制器、可丢弃、能验证的工作负载。"""
    problems: list[str] = []
    if proposal.target.kind != "Pod":
        problems.append(f"restart_pod 的目标类型必须是 Pod：{proposal.target.kind}")
    parameters = proposal.parameters
    if parameters.get("owner_kind") != "ReplicaSet":
        problems.append("目标 Pod 没有 ReplicaSet 归属，删除后没有控制器重建")
    if parameters.get("discardable") is not True:
        problems.append("目标 Pod 未标记为可丢弃工作负载")
    if not parameters.get("service"):
        problems.append("缺少用于验证恢复的 Service")
    return problems


def decide(
    proposal: ActionProposal, *, target_busy: bool, prior_auto_failure: bool = False
) -> RiskDecision:
    """只做规则判断，不接受模型自评分。任何硬约束不满足都直接拒绝。"""
    reasons: list[str] = []
    if proposal.target.namespace not in ALLOWED_NAMESPACES:
        reasons.append(f"命名空间不在白名单：{proposal.target.namespace}")
    if proposal.kind not in ALLOWED_KINDS:
        reasons.append(f"动作未注册：{proposal.kind.value}")
    if target_busy:
        reasons.append("目标已有未完成的执行")
    if proposal.kind is ActionKind.RESTART_POD:
        reasons.extend(_restart_problems(proposal))

    if reasons:
        return RiskDecision(
            proposal_id=proposal.id,
            disposition=RiskDisposition.DENY,
            policy_version=POLICY_VERSION,
            reasons=reasons,
            requires_compensation=False,
        )

    # 自动执行规则：只覆盖实验命名空间里的 Pod 重建。硬约束（可丢弃、有控制器、
    # 有验证 Service）已经在上面判过，这里不再看动作以外的信号。
    if (
        proposal.kind is ActionKind.RESTART_POD
        and proposal.target.namespace in AUTO_NAMESPACES
        and not prior_auto_failure
    ):
        return RiskDecision(
            proposal_id=proposal.id,
            disposition=RiskDisposition.AUTO,
            policy_version=POLICY_VERSION,
            reasons=[
                "动作 restart_pod 命中自动执行规则",
                f"目标在专用实验命名空间：{proposal.target.namespace}",
                "目标 Pod 可丢弃，删除后由 ReplicaSet 重建",
                "执行后按 Pod 与 Service 验证恢复",
            ],
            requires_compensation=False,
        )

    reasons = [
        f"动作 {proposal.kind.value}",
        "目标在实验命名空间",
        "首次执行需人工审批",
    ]
    if prior_auto_failure:
        reasons.append("同一工作负载上一次执行失败或结果未知，本次转人工审批")
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
