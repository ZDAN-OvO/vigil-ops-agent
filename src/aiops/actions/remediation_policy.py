"""自动修复登记、配置装载和证据契约。"""

import hashlib
import json
import math
import re
from datetime import datetime
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from loguru import logger
from pydantic import (
    AwareDatetime,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)
from yaml.nodes import MappingNode

from aiops.contracts import (
    ActionKind,
    ActionParameters,
    ActionTarget,
    AdjustResourcesParams,
    Contract,
    ScaleReplicasParams,
    UpdateConfigParams,
    parse_action_parameters,
)

ALLOWED_NAMESPACES = frozenset({"vigil-shop", "vigil-experiment"})
CONTROLLED_KINDS = frozenset(
    {ActionKind.ADJUST_RESOURCES, ActionKind.SCALE_REPLICAS, ActionKind.UPDATE_CONFIG}
)
RECONCILIATION_ANNOTATION = "vigil.aiops/reconciliation"
REMEDIATION_IDENTITY = "system:serviceaccount:vigil-monitoring:vigil-remediation"
RESOURCE_FIELDS = ("request_cpu", "limit_cpu", "request_memory", "limit_memory")

PositiveInt = Annotated[StrictInt, Field(gt=0)]
Scalar = StrictBool | StrictInt | StrictFloat | str
_NON_ORDINARY_FIELD = re.compile(
    r"SECRET|PASSWORD|PASSWD|TOKEN|CREDENTIAL|PRIVATE|KEY|CERT|AUTH|COOKIE|"
    r"DATABASE|DBURL|DSN|CONNECTION|ENDPOINT|URL|ROLE|PERMISSION|POLICY|"
    r"COMMAND|ENTRYPOINT|EXEC|SCRIPT|ARGS|LD_PRELOAD"
)
_ORDINARY_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", re.ASCII)
_QUANTITY = re.compile(
    r"([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))"
    r"(n|u|m|k|M|G|T|P|E|Ki|Mi|Gi|Ti|Pi|Ei|[eE][+-]?[0-9]+)?"
)
_DECIMAL_SCALES = {
    "n": -9,
    "u": -6,
    "m": -3,
    "": 0,
    "k": 3,
    "M": 6,
    "G": 9,
    "T": 12,
    "P": 15,
    "E": 18,
}
_BINARY_SCALES = {
    suffix: 1024**power
    for power, suffix in enumerate(("Ki", "Mi", "Gi", "Ti", "Pi", "Ei"), start=1)
}


def resource_quantity(value: str, *, cpu: bool) -> Decimal:
    """将 Kubernetes 数量转为核或字节，以相同单位判断边界。"""
    match = _QUANTITY.fullmatch(value) if len(value) <= 40 else None
    if match is None:
        raise ValueError("Kubernetes 资源数量格式无效")
    number, suffix = match.groups()
    with localcontext(prec=80):
        amount = Decimal(number)
        suffix = suffix or ""
        if suffix in _DECIMAL_SCALES:
            amount = amount.scaleb(_DECIMAL_SCALES[suffix])
        elif suffix in _BINARY_SCALES:
            amount *= _BINARY_SCALES[suffix]
        else:
            exponent = int(suffix[1:])
            if not -30 <= exponent <= 30:
                raise ValueError("资源数量指数越界")
            amount = amount.scaleb(exponent)
        if amount <= 0 or amount >= 2**63:
            raise ValueError("资源数量必须为 API 可表示的正数")
        precision = amount * (1000 if cpu else 1_000_000_000)
        if precision != precision.to_integral_value():
            raise ValueError("资源数量超过 API 支持的精度")
        return amount


class PolicyModel(Contract):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)


class QuantityBounds(PolicyModel):
    auto_min: str
    auto_max: str
    hard_max: str

    def amounts(self, field: str) -> tuple[Decimal, Decimal, Decimal]:
        return (
            resource_quantity(self.auto_min, cpu=field.endswith("_cpu")),
            resource_quantity(self.auto_max, cpu=field.endswith("_cpu")),
            resource_quantity(self.hard_max, cpu=field.endswith("_cpu")),
        )


class ReplicaBounds(PolicyModel):
    min: PositiveInt
    max: PositiveInt

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if not self.min <= self.max <= 2**31 - 1:
            raise ValueError("副本数范围无效")
        return self


class ConfigField(PolicyModel):
    source: Literal["env", "config_map"]
    key: str
    container: str | None = None
    config_map: str | None = None
    allowed_values: tuple[str, ...] = Field(min_length=1)
    auto_values: tuple[str, ...] = ()
    load_signal_promql: str = Field(min_length=1)

    def load_signal_for(self, value: str) -> str | None:
        if value not in self.allowed_values:
            return None
        return self.load_signal_promql.replace("{value}", value)

    @model_validator(mode="after")
    def validate_values(self) -> Self:
        params = UpdateConfigParams.model_validate(
            self.model_dump(
                exclude={"allowed_values", "auto_values", "load_signal_promql"}, exclude_none=True
            )
            | {"value": ""}
        )
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", params.key) or _NON_ORDINARY_FIELD.search(
            re.sub(r"[^A-Z0-9]", "", params.key.upper())
        ):
            raise ValueError("配置字段不属于普通配置")
        if (
            len(set(self.allowed_values)) != len(self.allowed_values)
            or len(set(self.auto_values)) != len(self.auto_values)
            or not set(self.auto_values) <= set(self.allowed_values)
            or self.load_signal_promql.count("{value}") != 1
            or any(not _ORDINARY_VALUE.fullmatch(value) for value in self.allowed_values)
        ):
            raise ValueError("普通配置值必须是明确登记的非敏感枚举值")
        return self

    @property
    def identity(self) -> tuple[str, str, str | None, str | None]:
        return self.source, self.key, self.container, self.config_map

    def matches(self, params: UpdateConfigParams) -> bool:
        return self.identity == (params.source, params.key, params.container, params.config_map)


class EvidencePredicate(PolicyModel):
    key: str = Field(min_length=1)
    source: Literal["current", "observed"] = "observed"
    operator: Literal["eq", "lt", "le", "gt", "ge"] = "eq"
    value: Scalar
    promql: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def verify_operator(self) -> Self:
        if self.source == "current" and self.promql is not None:
            raise ValueError("现场值不能由 PromQL 提供")
        if self.operator != "eq" and type(self.value) not in (int, float):
            raise ValueError("大小比较要求数值阈值")
        if type(self.value) is float and not math.isfinite(self.value):
            raise ValueError("数值证据阈值必须有限")
        return self

    def matches(self, value: object) -> bool:
        if type(self.value) in (int, float):
            if type(value) not in (int, float, Decimal):
                return False
            amount = Decimal(str(value))
            if not amount.is_finite():
                return False
            threshold = Decimal(str(self.value))
            return {
                "eq": amount == threshold,
                "lt": amount < threshold,
                "le": amount <= threshold,
                "gt": amount > threshold,
                "ge": amount >= threshold,
            }[self.operator]
        return type(value) is type(self.value) and value == self.value


class ProbePolicy(PolicyModel):
    promql: str = Field(min_length=1)
    success_value: Literal[1] = 1
    observation_seconds: PositiveInt = 600
    sample_seconds: PositiveInt = 60

    @model_validator(mode="after")
    def verify_intervals(self) -> Self:
        if self.observation_seconds % self.sample_seconds:
            raise ValueError("观察窗口必须包含整数个采样时段")
        return self


class BaseRulePolicy(PolicyModel):
    id: str = Field(min_length=1)
    kind: ActionKind
    parameters: ActionParameters
    evidence: tuple[EvidencePredicate, ...] = Field(min_length=1)
    max_evidence_age_seconds: PositiveInt
    worsening: EvidencePredicate | None = None
    probe: ProbePolicy | None = None

    @model_validator(mode="before")
    @classmethod
    def parse_parameters(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("parameters"), dict):
            return data | {
                "parameters": parse_action_parameters(ActionKind(data["kind"]), data["parameters"])
            }
        return data

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if len({item.key for item in self.evidence}) != len(self.evidence):
            raise ValueError("规则存在重复证据键")
        return self


class RulePolicy(BaseRulePolicy):
    type: Literal["alert"] = "alert"
    alertname: str = Field(min_length=1)
    labels: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_labels(self) -> Self:
        if any(
            not key
            or not value
            or key in {"alertname", "namespace", "target", "workload_kind", "cluster"}
            for key, value in self.labels.items()
        ):
            raise ValueError("规则标签重复约束了目标标识或为空")
        return self


WorkloadCheck = Literal["workload-readiness", "workload-signal"]


class InspectionPredicate(EvidencePredicate):
    check_id: WorkloadCheck
    source: Literal["observed"] = "observed"
    promql: None = None

    @model_validator(mode="after")
    def validate_check_key(self) -> Self:
        keys = {"value", "desired_replicas", "ready_replicas"}
        if self.key not in (keys if self.check_id == "workload-readiness" else {"value"}):
            raise ValueError("观察条件引用未登记的字段")
        return self


class InspectionRulePolicy(BaseRulePolicy):
    type: Literal["inspection"]
    check_id: WorkloadCheck
    priority: PositiveInt
    observations: tuple[InspectionPredicate, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_inspection(self) -> Self:
        keys = {(item.check_id, item.key) for item in self.observations}
        if len(keys) != len(self.observations) or self.check_id not in {
            item.check_id for item in self.observations
        }:
            raise ValueError("巡检规则必须约束触发检查且观察字段不可重复")
        if any(item.source != "current" for item in self.evidence):
            raise ValueError("巡检规则的外部观察须引用已登记检查")
        return self

    @property
    def required_checks(self) -> set[str]:
        return {"workload-readiness", *(item.check_id for item in self.observations)}


RemediationRule = RulePolicy | InspectionRulePolicy


class ServicePolicy(PolicyModel):
    namespace: str
    kind: Literal["Deployment", "StatefulSet"]
    name: str
    containers: tuple[str, ...] = Field(min_length=1)
    resources: dict[str, dict[str, QuantityBounds]] = Field(default_factory=dict)
    replicas: ReplicaBounds | None = None
    config_fields: tuple[ConfigField, ...] = ()
    probe: ProbePolicy
    rules: tuple[RemediationRule, ...] = ()

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.namespace, self.kind, self.name

    def config_field(self, params: UpdateConfigParams) -> ConfigField | None:
        return next((field for field in self.config_fields if field.matches(params)), None)

    @model_validator(mode="after")
    def validate_fields(self) -> Self:
        if self.namespace not in ALLOWED_NAMESPACES:
            raise ValueError("服务命名空间不在应用硬边界内")
        if not re.fullmatch(r"[a-z0-9](?:[-.a-z0-9]*[a-z0-9])?", self.name):
            raise ValueError("服务名称无效")
        if len(set(self.containers)) != len(self.containers) or not all(
            re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", container)
            for container in self.containers
        ):
            raise ValueError("容器未登记或重复")
        if not self.resources.keys() <= set(self.containers):
            raise ValueError("资源规格引用未登记的容器")
        for fields in self.resources.values():
            for field, bounds in fields.items():
                if field not in RESOURCE_FIELDS:
                    raise ValueError("资源规格字段未登记")
                minimum, maximum, hard_max = bounds.amounts(field)
                if not minimum <= maximum <= hard_max:
                    raise ValueError("资源规格的 AUTO 区间和硬上限冲突")
        if len({field.identity for field in self.config_fields}) != len(self.config_fields):
            raise ValueError("服务普通配置重复")
        for field in self.config_fields:
            if field.source == "env" and field.container not in self.containers:
                raise ValueError("环境变量引用未登记的容器")
        alerts = []
        priorities = set()
        for rule in self.rules:
            if isinstance(rule, RulePolicy):
                for former in alerts:
                    overlap = not any(
                        key in former.labels and value != former.labels[key]
                        for key, value in rule.labels.items()
                    )
                    if rule.alertname == former.alertname and overlap:
                        raise ValueError("同一告警与目标匹配了多条规则")
                alerts.append(rule)
            else:
                if rule.priority in priorities:
                    raise ValueError("同一目标的巡检规则优先级必须唯一")
                priorities.add(rule.priority)
            self._validate_rule_parameters(rule)
        return self

    def _validate_rule_parameters(self, rule: RemediationRule) -> None:
        params = rule.parameters
        if isinstance(params, AdjustResourcesParams):
            local = {
                f"{field}_millicores" if field.endswith("_cpu") else f"{field}_bytes"
                for field in RESOURCE_FIELDS
            }
        elif isinstance(params, UpdateConfigParams):
            local = {"current_value"}
        else:
            local = {"replicas"}
        if any(item.source == "current" and item.key not in local for item in rule.evidence):
            raise ValueError("规则引用未定义的现场证据键")
        if isinstance(params, AdjustResourcesParams):
            bounds_by_field = self.resources.get(params.container, {})
            changed = {field: getattr(params, field) for field in RESOURCE_FIELDS}
            for field, value in changed.items():
                if value is not None:
                    bounds = bounds_by_field.get(field)
                    if bounds is None:
                        raise ValueError("规则使用未登记的资源规格")
                    minimum, maximum, _ = bounds.amounts(field)
                    if (
                        not minimum
                        <= resource_quantity(value, cpu=field.endswith("_cpu"))
                        <= maximum
                    ):
                        raise ValueError("规则指定的资源规格超出 AUTO 区间")
            for suffix in ("cpu", "memory"):
                request, limit = changed[f"request_{suffix}"], changed[f"limit_{suffix}"]
                if (
                    request is not None
                    and limit is not None
                    and resource_quantity(request, cpu=suffix == "cpu")
                    > resource_quantity(limit, cpu=suffix == "cpu")
                ):
                    raise ValueError("规则的 request 超过 limit")
        elif isinstance(params, UpdateConfigParams):
            field = self.config_field(params)
            if field is None or params.value not in field.auto_values:
                raise ValueError("规则指定的普通配置值不在 AUTO 白名单")
        elif self.replicas is None or not self.replicas.min <= params.replicas <= self.replicas.max:
            raise ValueError("规则指定的副本数超出 AUTO 区间")


class GlobalPolicy(PolicyModel):
    schema_version: Literal[1]
    revision: str = Field(min_length=1)
    forbidden_config_keys: tuple[str, ...] = ()

    @field_validator("forbidden_config_keys")
    @classmethod
    def normalize_keys(cls, keys: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(key.casefold() for key in keys)


class RemediationPolicy(PolicyModel):
    version: str
    services: tuple[ServicePolicy, ...] = ()
    forbidden_config_keys: tuple[str, ...] = ()
    error: str | None = None

    def service_for(self, target: ActionTarget) -> ServicePolicy | None:
        if self.error is not None:
            return None
        key = (target.namespace, target.kind, target.name)
        return next((service for service in self.services if service.identity == key), None)

    def match_rule(self, target: ActionTarget, labels: dict[str, str]) -> RulePolicy | None:
        service = self.service_for(target)
        if (
            service is None
            or labels.get("namespace") != target.namespace
            or labels.get("target") != target.name
            or labels.get("workload_kind", target.kind) != target.kind
            or labels.get("cluster", target.cluster) != target.cluster
        ):
            return None
        return next(
            (
                rule
                for rule in service.rules
                if isinstance(rule, RulePolicy)
                and rule.alertname == labels.get("alertname")
                and all(labels.get(key) == value for key, value in rule.labels.items())
            ),
            None,
        )

    def inspection_rules(self, target: ActionTarget) -> tuple[InspectionRulePolicy, ...]:
        service = self.service_for(target)
        if service is None:
            return ()
        return tuple(
            sorted(
                (rule for rule in service.rules if isinstance(rule, InspectionRulePolicy)),
                key=lambda rule: rule.priority,
                reverse=True,
            )
        )

    def rule_for_evidence(
        self, target: ActionTarget, evidence: "TrustedEvidence"
    ) -> RemediationRule | None:
        if evidence.source == "alert":
            return self.match_rule(target, evidence.labels)
        return next(
            (rule for rule in self.inspection_rules(target) if rule.id == evidence.rule_id), None
        )


class CurrentState(PolicyModel):
    target: ActionTarget
    parameters: ActionParameters
    config_map_exclusive: StrictBool = False

    def evidence_values(self) -> dict[str, object]:
        params = self.parameters
        if isinstance(params, ScaleReplicasParams):
            return {"replicas": params.replicas}
        if isinstance(params, UpdateConfigParams):
            return {"current_value": params.value}
        result: dict[str, object] = {}
        for field in RESOURCE_FIELDS:
            value = getattr(params, field)
            if value is not None:
                cpu = field.endswith("_cpu")
                amount = resource_quantity(value, cpu=cpu)
                result[f"{field}_millicores" if cpu else f"{field}_bytes"] = (
                    amount * 1000 if cpu else amount
                )
        return result


class TrustedEvidence(PolicyModel):
    rule_id: str
    policy_version: str
    labels: dict[str, str]
    evidence_ids: tuple[str, ...]
    observed_at: AwareDatetime
    values: dict[str, Any]
    source: Literal["alert", "inspection"] = "alert"
    inspection_values: dict[str, dict[str, Any]] = Field(default_factory=dict)

    def satisfies(self, rule: RemediationRule, now: datetime) -> bool:
        age = (now - self.observed_at).total_seconds()
        if not 0 <= age <= rule.max_evidence_age_seconds or not all(
            predicate.matches(self.values.get(predicate.key)) for predicate in rule.evidence
        ):
            return False
        if isinstance(rule, InspectionRulePolicy):
            return all(
                predicate.matches(
                    self.inspection_values.get(predicate.check_id, {}).get(predicate.key)
                )
                for predicate in rule.observations
            )
        return True


class CompensationContext(PolicyModel):
    original: ActionParameters
    written: ActionParameters
    policy_version: str


class UniqueKeyLoader(yaml.SafeLoader):
    """重复 YAML 键属于配置错误，禁止后一个定义静默覆盖。"""

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict:
        self.flatten_mapping(node)
        mapping = super().construct_mapping(node, deep=deep)
        if len(mapping) != len(node.value):
            raise ValueError("YAML 存在重复键")
        return mapping


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ValueError(f"{path.name} 无法读取或包含无效 YAML") from exc


def _read_services(path: Path) -> tuple[ServicePolicy, ...]:
    return tuple(
        ServicePolicy.model_validate(_read_yaml(item)) for item in sorted(path.glob("*.yaml"))
    )


def _load_policy(path: Path) -> RemediationPolicy:
    global_path = path
    profile = path / "profile.yaml"
    if profile.is_file():
        # 隔离验收显式选择独立目录，继承相邻的正式策略，按服务替换。
        if _read_yaml(profile) != {"profile": "isolated-acceptance"}:
            raise ValueError("未知的修复策略配置")
        global_path = path.parent
    config = GlobalPolicy.model_validate(_read_yaml(global_path / "global.yaml"))
    services = _read_services(global_path / "services")
    if global_path != path:
        overrides = _read_services(path / "services")
        by_target = {service.identity: service for service in overrides}
        if (
            not overrides
            or len(by_target) != len(overrides)
            or not by_target.keys() <= {service.identity for service in services}
        ):
            raise ValueError("隔离配置只可替换现有且唯一的登记服务")
        services = tuple(by_target.get(service.identity, service) for service in services)
    if not services or len({service.identity for service in services}) != len(services):
        raise ValueError("缺少服务策略或存在重复目标")
    rule_ids: set[str] = set()
    owners: dict[tuple[str, str], tuple[str, str, str]] = {}
    for service in services:
        for rule in service.rules:
            if rule.id in rule_ids:
                raise ValueError("规则 ID 重复")
            rule_ids.add(rule.id)
        for field in service.config_fields:
            if field.key.casefold() in config.forbidden_config_keys:
                raise ValueError("服务登记了全局禁改字段")
            if field.config_map is not None:
                key = (service.namespace, field.config_map)
                if owners.setdefault(key, service.identity) != service.identity:
                    raise ValueError("ConfigMap 必须由单一登记服务独占")
    canonical_services = [service.model_dump(mode="json") for service in services]
    # 旧告警策略的准入版本包含原来的完整默认值。
    for service in canonical_services:
        for rule in service["rules"]:
            if rule["type"] == "alert":
                del rule["type"]
    canonical = {"global": config.model_dump(mode="json"), "services": canonical_services}
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()
    return RemediationPolicy(
        version=f"{config.revision}-{digest}",
        services=services,
        forbidden_config_keys=config.forbidden_config_keys,
    )


def load_policy(path: Path | None) -> RemediationPolicy:
    """加载失败时关闭写入授权，保留可记录的原因。"""
    try:
        if path is None:
            raise ValueError("未配置 VIGIL_REMEDIATION_CONFIG_DIR")
        return _load_policy(path)
    except (OSError, ValueError, KeyError) as exc:
        reason = str(exc)
        logger.warning("修复策略不可用：{}", reason)
        return RemediationPolicy(version="unavailable", error=reason)
