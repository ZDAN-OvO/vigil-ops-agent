"""提案和发送授权共用的风险决策。"""

from datetime import UTC, datetime

from aiops.actions.remediation_policy import (
    ALLOWED_NAMESPACES,
    CONTROLLED_KINDS,
    RESOURCE_FIELDS,
    RulePolicy,
    ServicePolicy,
    resource_quantity,
)
from aiops.actions.remediation_policy import (
    CompensationContext as CompensationContext,
)
from aiops.actions.remediation_policy import (
    CurrentState as CurrentState,
)
from aiops.actions.remediation_policy import (
    RemediationPolicy as RemediationPolicy,
)
from aiops.actions.remediation_policy import (
    TrustedEvidence as TrustedEvidence,
)
from aiops.actions.remediation_policy import (
    load_policy as load_policy,
)
from aiops.contracts import (
    ActionOrigin,
    ActionParameters,
    ActionProposal,
    AdjustResourcesParams,
    RiskDecision,
    RiskDisposition,
    ScaleReplicasParams,
    UpdateConfigParams,
    parse_action_parameters,
)

POLICY_VERSION = "unavailable"
# 兼容旧执行模块导入。旧写入无可取得授权的自动命名空间。
AUTO_NAMESPACES: frozenset[str] = frozenset()


def _changes(params: ActionParameters) -> dict[str, object]:
    if isinstance(params, AdjustResourcesParams):
        return {
            field: resource_quantity(value, cpu=field.endswith("_cpu"))
            for field in RESOURCE_FIELDS
            if (value := getattr(params, field)) is not None
        }
    if isinstance(params, ScaleReplicasParams):
        return {"replicas": params.replicas}
    return {"value": params.value}


def _location(params: ActionParameters) -> tuple:
    if isinstance(params, AdjustResourcesParams):
        return (type(params), params.container)
    if isinstance(params, ScaleReplicasParams):
        return (type(params),)
    return type(params), params.source, params.key, params.container, params.config_map


def _same_change(left: ActionParameters, right: ActionParameters) -> bool:
    return _location(left) == _location(right) and _changes(left) == _changes(right)


def _resource_constraints(
    params: AdjustResourcesParams, before: AdjustResourcesParams, service: ServicePolicy
) -> bool:
    changed = _changes(params)
    original = _changes(before)
    available = service.resources.get(params.container, {})
    if not changed.keys() <= available.keys() or not changed.keys() <= original.keys():
        raise ValueError("资源规格未登记或缺少当前原值")
    after = original | changed
    for unit in ("cpu", "memory"):
        request = after.get(f"request_{unit}")
        limit = after.get(f"limit_{unit}")
        if request is not None and limit is not None and request > limit:
            raise ValueError("最终 request 超过 limit")
    eligible = True
    for field, value in changed.items():
        minimum, maximum, hard_max = available[field].amounts(field)
        if value > hard_max:
            raise ValueError("资源规格超过登记的硬上限")
        eligible = eligible and minimum <= value <= maximum
    return eligible


def _constraints(params: ActionParameters, current: CurrentState, service: ServicePolicy) -> bool:
    original = current.parameters
    if _location(params) != _location(original):
        raise ValueError("当前原值属于其他字段")
    changed = _changes(params)
    existing = _changes(original)
    if all(existing.get(field) == value for field, value in changed.items()):
        raise ValueError("目标值未发生变化")
    if isinstance(params, AdjustResourcesParams) and isinstance(original, AdjustResourcesParams):
        return _resource_constraints(params, original, service)
    if isinstance(params, ScaleReplicasParams) and isinstance(original, ScaleReplicasParams):
        if service.replicas is None or not 1 <= params.replicas <= service.replicas.max:
            raise ValueError("副本数超过硬边界或该服务未开放实例数变更")
        if original.replicas < 0:
            raise ValueError("无法核实当前实例数")
        return params.replicas > original.replicas and params.replicas >= service.replicas.min
    if isinstance(params, UpdateConfigParams) and isinstance(original, UpdateConfigParams):
        field = service.config_field(params)
        if (
            field is None
            or params.value not in field.allowed_values
            or original.value not in field.allowed_values
        ):
            raise ValueError("配置字段或值未登记为普通配置")
        if params.source == "config_map" and not current.config_map_exclusive:
            raise ValueError("无法确认 ConfigMap 由登记服务独占")
        return params.value in field.auto_values
    raise ValueError("当前原值类型与动作不一致")


def _trusted_rule(
    proposal: ActionProposal,
    policy: RemediationPolicy,
    params: ActionParameters,
    evidence: TrustedEvidence | None,
    current: CurrentState,
    now: datetime,
) -> RulePolicy | None:
    if (
        proposal.origin is not ActionOrigin.RULES
        or not proposal.investigation_id
        or evidence is None
        or evidence.policy_version != policy.version
        or not evidence.evidence_ids
        or set(evidence.evidence_ids) != set(proposal.evidence_ids)
        or evidence.labels.get("cluster", proposal.target.cluster) != proposal.target.cluster
        or now.utcoffset() is None
    ):
        return None
    rule = policy.match_rule(proposal.target, evidence.labels)
    if (
        rule is None
        or rule.id != evidence.rule_id
        or rule.kind is not proposal.kind
        or not _same_change(rule.parameters, params)
        or not evidence.satisfies(rule, now)
    ):
        return None
    actual = current.evidence_values()
    if any(
        predicate.source == "current" and not predicate.matches(actual.get(predicate.key))
        for predicate in rule.evidence
    ):
        return None
    return rule


def _valid_compensation(
    params: ActionParameters,
    current: CurrentState,
    compensation: CompensationContext,
) -> bool:
    if not _same_change(params, compensation.original):
        return False
    written = compensation.written
    expected = _changes(written)
    actual = _changes(current.parameters)
    return (
        _location(params) == _location(written)
        and _changes(params).keys() == expected.keys()
        and all(actual.get(field) == value for field, value in expected.items())
    )


def decide(
    proposal: ActionProposal,
    *,
    target_busy: bool,
    prior_auto_failure: bool = False,
    policy: RemediationPolicy | None = None,
    current: CurrentState | None = None,
    evidence: TrustedEvidence | None = None,
    auto_enabled: bool = False,
    compensation: CompensationContext | None = None,
    now: datetime | None = None,
) -> RiskDecision:
    """先核验共同硬边界，再校验自动处置或补偿的授权。"""
    version = policy.version if policy is not None else POLICY_VERSION

    def decision(
        disposition: RiskDisposition, reason: str, *, requires_compensation: bool = False
    ) -> RiskDecision:
        return RiskDecision(
            proposal_id=proposal.id,
            disposition=disposition,
            policy_version=version,
            reasons=[reason],
            requires_compensation=requires_compensation,
        )

    if proposal.kind not in CONTROLLED_KINDS:
        return decision(RiskDisposition.DENY, f"动作已停用：{proposal.kind.value}")
    if proposal.target.namespace not in ALLOWED_NAMESPACES:
        return decision(RiskDisposition.DENY, "目标命名空间不在白名单")
    if target_busy:
        return decision(RiskDisposition.DENY, "目标已有未完成的执行")
    if policy is None or policy.error is not None:
        reason = policy.error if policy is not None else "未加载修复策略"
        return decision(RiskDisposition.DENY, f"修复策略不可用：{reason}")
    service = policy.service_for(proposal.target)
    if service is None:
        return decision(RiskDisposition.DENY, "目标服务未登记")
    if (
        current is None
        or current.target != proposal.target
        or not all((proposal.target.uid, proposal.target.resource_version, proposal.target.cluster))
    ):
        return decision(RiskDisposition.DENY, "无法核实目标 UID、版本及当前原值")

    try:
        params = parse_action_parameters(proposal.kind, proposal.parameters)
    except ValueError:
        return decision(RiskDisposition.DENY, "动作参数不符合契约")

    try:
        automatic_range = _constraints(params, current, service)
        if compensation is not None:
            if (
                auto_enabled
                and proposal.origin is ActionOrigin.RULES
                and compensation.policy_version == version
                and _valid_compensation(params, current, compensation)
            ):
                return decision(RiskDisposition.AUTO, "恢复已核实且符合硬边界的原值")
            return decision(RiskDisposition.DENY, "补偿缺少与正向写入一致的可信原值或授权")
    except ValueError as exc:
        return decision(RiskDisposition.DENY, str(exc))

    if not auto_enabled:
        return decision(RiskDisposition.APPROVAL, "自动处置开关已关闭")
    if prior_auto_failure:
        return decision(RiskDisposition.APPROVAL, "本次故障已有失败或待核实的自动执行")
    if not automatic_range:
        return decision(RiskDisposition.APPROVAL, "参数需人工审查")
    rule = _trusted_rule(proposal, policy, params, evidence, current, now or datetime.now(UTC))
    if rule is None:
        return decision(RiskDisposition.APPROVAL, "普通变更或缺少满足规则的可信证据")
    return decision(
        RiskDisposition.AUTO,
        f"命中自动处置规则 {rule.id}",
        requires_compensation=True,
    )


def target_key(cluster: str, namespace: str, kind: str, name: str) -> str:
    return f"{cluster}/{namespace}/{kind}/{name}"
