"""三档判定、故障证据、补偿和 YAML 配置的单元回归。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from shutil import copytree
from typing import Any

import pytest
import yaml

from aiops.actions.policy import (
    CompensationContext,
    CurrentState,
    TrustedEvidence,
    decide,
    load_policy,
)
from aiops.actions.remediation_policy import resource_quantity
from aiops.contracts import (
    ActionKind,
    ActionOrigin,
    ActionProposal,
    ActionTarget,
    AdjustResourcesParams,
    RiskDisposition,
    ScaleReplicasParams,
    UpdateConfigParams,
    parse_action_parameters,
)

CONFIG = Path(__file__).resolve().parents[1] / "config/remediation"
NOW = datetime(2026, 1, 1, tzinfo=UTC)
DEFAULT_VALUES = {
    "currency-memory-oom": {
        "limit_memory_bytes": 16 * 1024**2,
        "oom_killed": True,
        "container_restarts": 3,
        "business_failed": 0.0,
    },
    "currency-cpu-limit": {
        "limit_cpu_millicores": 5,
        "throttle_ratio": 0.55,
        "business_failed": 0.0,
    },
    "replica-capacity": {
        "replicas": 1,
        "rejected_requests": 1,
        "ready_endpoints": 1,
        "business_failed": 0.0,
    },
    "config-service-mode": {"current_value": "degraded", "gate_failed": 0.0},
    "isolated-config-worsening": {"current_value": "degraded", "gate_failed": 0.0},
    "config-env-mode": {"current_value": "degraded", "gate_failed": 0.0},
}
CURRENT = {
    "currency-memory-oom": AdjustResourcesParams(
        container="server",
        request_cpu="100m",
        limit_cpu="200m",
        request_memory="8Mi",
        limit_memory="16Mi",
    ),
    "currency-cpu-limit": AdjustResourcesParams(
        container="server",
        request_cpu="5m",
        limit_cpu="5m",
        request_memory="64Mi",
        limit_memory="128Mi",
    ),
    "replica-capacity": ScaleReplicasParams(replicas=1),
    "config-service-mode": UpdateConfigParams(
        source="config_map", key="mode", value="degraded", config_map="vigil-config-fixture"
    ),
}
CURRENT["config-env-mode"] = UpdateConfigParams(
    source="env", key="VIGIL_MODE", value="degraded", container="app"
)
CURRENT["isolated-config-worsening"] = CURRENT["config-service-mode"]


def scenario(rule_id, *, config=CONFIG):
    policy = load_policy(config)
    assert policy.error is None
    service, rule = next(
        (service, rule)
        for service in policy.services
        for rule in service.rules
        if rule.id == rule_id
    )
    target = ActionTarget(
        cluster="vigil-lab",
        namespace=service.namespace,
        kind=service.kind,
        name=service.name,
        uid="uid",
        resource_version="4",
    )
    proposal = ActionProposal(
        id="proposal",
        kind=rule.kind,
        target=target,
        parameters=rule.parameters.model_dump(exclude_none=True),
        idempotency_key="once",
        reason="规则评估",
        created_at=NOW,
        origin=ActionOrigin.RULES,
        investigation_id="investigation",
        evidence_ids=["ev"],
    )
    current = CurrentState(target=target, parameters=CURRENT[rule.id], config_map_exclusive=True)
    evidence = TrustedEvidence(
        rule_id=rule.id,
        policy_version=policy.version,
        labels={
            "alertname": rule.alertname,
            "namespace": service.namespace,
            "target": service.name,
            **rule.labels,
        },
        evidence_ids=("ev",),
        observed_at=NOW,
        values=DEFAULT_VALUES[rule.id],
    )
    return proposal, {
        "policy": policy,
        "current": current,
        "evidence": evidence,
        "auto_enabled": True,
        "target_busy": False,
        "now": NOW,
    }


@pytest.mark.parametrize("rule", tuple(CURRENT)[:-1])
def test_trusted_rule_can_auto_apply(rule):
    proposal, context = scenario(rule)
    result = decide(proposal, **context)

    assert result.disposition is RiskDisposition.AUTO
    assert result.policy_version == context["policy"].version
    assert rule in result.reasons[0]
    assert result.requires_compensation


@pytest.mark.parametrize(
    ("change",),
    [
        ({"observed_at": NOW - timedelta(seconds=121)},),
        ({"observed_at": NOW + timedelta(seconds=1)},),
        ({"policy_version": "old"},),
        ({"rule_id": "other"},),
        ({"evidence_ids": ("not-in-investigation",)},),
        ({"labels": {"alertname": "FrontendProbeFailed"}},),
        ({"values": DEFAULT_VALUES["currency-memory-oom"] | {"oom_killed": 1}},),
    ],
)
def test_stale_conflicting_or_unbound_evidence_requires_approval(change):
    proposal, context = scenario("currency-memory-oom")
    context["evidence"] = context["evidence"].model_copy(update=change)

    assert decide(proposal, **context).disposition is RiskDisposition.APPROVAL


@pytest.mark.parametrize("origin", [ActionOrigin.MODEL, ActionOrigin.OPERATOR])
def test_models_and_operators_cannot_reuse_trusted_rule_authorization(origin):
    proposal, context = scenario("currency-memory-oom")

    assert (
        decide(proposal.model_copy(update={"origin": origin}), **context).disposition
        is RiskDisposition.APPROVAL
    )


def test_send_rechecks_actual_original_against_evidence():
    proposal, context = scenario("replica-capacity")
    altered = proposal.model_copy(update={"parameters": {"replicas": 3}})
    context["current"] = context["current"].model_copy(
        update={"parameters": ScaleReplicasParams(replicas=0)}
    )

    assert decide(proposal, **context).disposition is RiskDisposition.APPROVAL
    assert decide(altered, **context).disposition is RiskDisposition.APPROVAL


def test_missing_configuration_and_drift_fail_closed():
    proposal, context = scenario("currency-memory-oom")
    assert decide(proposal, target_busy=False).disposition is RiskDisposition.DENY
    assert decide(proposal, **(context | {"current": None})).disposition is RiskDisposition.DENY
    current = context["current"]
    drift = current.model_copy(update={"target": current.target.model_copy(update={"uid": "new"})})
    assert decide(proposal, **(context | {"current": drift})).disposition is RiskDisposition.DENY
    assert (
        decide(proposal, **(context | {"policy": load_policy(None)})).disposition
        is RiskDisposition.DENY
    )


def test_busy_or_undeclared_target_is_denied():
    proposal, context = scenario("currency-memory-oom")
    assert decide(proposal, **(context | {"target_busy": True})).disposition is RiskDisposition.DENY
    target = proposal.target.model_copy(update={"name": "other"})
    modified = proposal.model_copy(update={"target": target})
    result = decide(modified, **context)
    assert result.disposition is RiskDisposition.DENY
    assert "未登记" in result.reasons[0]


@pytest.mark.parametrize("switch", [{"auto_enabled": False}, {"prior_auto_failure": True}])
def test_disabled_auto_or_previous_failure_requires_approval(switch):
    proposal, context = scenario("currency-memory-oom")
    assert decide(proposal, **(context | switch)).disposition is RiskDisposition.APPROVAL


@pytest.mark.parametrize("kind", [ActionKind.RESTART_POD, ActionKind.ROLLOUT_UNDO])
def test_legacy_proposal_can_be_read_but_cannot_write(kind):
    proposal, context = scenario("currency-memory-oom")
    legacy = ActionProposal.model_validate(
        proposal.model_dump() | {"kind": kind, "parameters": {"legacy_extra": "value"}}
    )

    assert decide(legacy, **context).disposition is RiskDisposition.DENY


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ({"limit_memory": "512Mi"}, RiskDisposition.APPROVAL),
        ({"limit_memory": "512001Ki"}, RiskDisposition.APPROVAL),
        ({"limit_memory": "513Mi"}, RiskDisposition.DENY),
        ({"limit_memory": "8191Ki"}, RiskDisposition.DENY),
        ({"limit_memory": "16000000"}, RiskDisposition.APPROVAL),
        ({"limit_memory": "1.2MB"}, RiskDisposition.DENY),
        ({"limit_cpu": "0.1001"}, RiskDisposition.DENY),
        ({"limit_cpu": "100m"}, RiskDisposition.APPROVAL),
        ({"limit_cpu": "1.001"}, RiskDisposition.DENY),
        ({"limit_cpu": "NaN"}, RiskDisposition.DENY),
        ({"limit_memory": "128Mi", "rule_id": "currency-memory-oom"}, RiskDisposition.DENY),
    ],
)
def test_resource_hard_bounds_apply_to_all_origins(candidate, expected):
    proposal, context = scenario("currency-memory-oom")
    proposal = proposal.model_copy(
        update={"parameters": {"container": "server", **candidate}, "origin": ActionOrigin.OPERATOR}
    )
    assert decide(proposal, **context).disposition is expected


def test_resource_quantity_form_does_not_change_auto_decision():
    proposal, context = scenario("currency-memory-oom")
    proposal = proposal.model_copy(
        update={"parameters": {"container": "server", "limit_memory": "131072Ki"}}
    )

    assert decide(proposal, **context).disposition is RiskDisposition.AUTO
    assert resource_quantity("0.1", cpu=True) == resource_quantity("100m", cpu=True)
    assert resource_quantity("64Mi", cpu=False) == resource_quantity("65536Ki", cpu=False)


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        (0, RiskDisposition.DENY),
        (-1, RiskDisposition.DENY),
        (True, RiskDisposition.DENY),
        ("2", RiskDisposition.DENY),
        (2.0, RiskDisposition.DENY),
        (4, RiskDisposition.DENY),
        (2, RiskDisposition.DENY),
        (1, RiskDisposition.APPROVAL),
        (3, RiskDisposition.APPROVAL),
    ],
)
def test_replica_bounds_and_human_approval(candidate: Any, expected):
    proposal, context = scenario("replica-capacity")
    proposal = proposal.model_copy(update={"parameters": {"replicas": candidate}})
    current = CurrentState(target=proposal.target, parameters=ScaleReplicasParams(replicas=2))

    assert decide(proposal, **(context | {"current": current})).disposition is expected


def test_config_map_must_be_exclusive_and_worsen_requires_isolated_policy():
    proposal, context = scenario("config-service-mode")
    shared = context["current"].model_copy(update={"config_map_exclusive": False})
    assert decide(proposal, **(context | {"current": shared})).disposition is RiskDisposition.DENY
    unsafe = proposal.model_copy(update={"parameters": proposal.parameters | {"value": "worsen"}})
    assert decide(unsafe, **context).disposition is RiskDisposition.DENY

    isolated, isolated_context = scenario(
        "isolated-config-worsening", config=CONFIG / "isolated-acceptance"
    )
    assert decide(isolated, **isolated_context).disposition is RiskDisposition.AUTO
    assert isolated_context["policy"].version != context["policy"].version
    assert isolated_context["policy"].services[0].identity == context["policy"].services[0].identity


def test_limited_env_value_requires_approval_and_field_must_be_registered():
    proposal, context = scenario("config-service-mode")
    ordinary = {"source": "env", "container": "app", "key": "VIGIL_MODE", "value": "limited"}
    current = CurrentState(
        target=proposal.target, parameters=UpdateConfigParams(**(ordinary | {"value": "degraded"}))
    )
    proposal = proposal.model_copy(update={"parameters": ordinary, "origin": ActionOrigin.OPERATOR})
    assert (
        decide(proposal, **(context | {"current": current})).disposition is RiskDisposition.APPROVAL
    )
    proposal = proposal.model_copy(update={"parameters": ordinary | {"key": "DB_PASSWORD"}})
    assert decide(proposal, **(context | {"current": current})).disposition is RiskDisposition.DENY


def test_compensation_restores_verified_original_even_below_auto_minimum():
    proposal, context = scenario("replica-capacity")
    proposal = proposal.model_copy(update={"parameters": {"replicas": 1}})
    current = context["current"].model_copy(update={"parameters": ScaleReplicasParams(replicas=2)})
    compensation = CompensationContext(
        original=ScaleReplicasParams(replicas=1),
        written=ScaleReplicasParams(replicas=2),
        policy_version=context["policy"].version,
    )
    context |= {"current": current, "compensation": compensation}
    assert decide(proposal, **context).disposition is RiskDisposition.AUTO

    denied = proposal.model_copy(update={"parameters": {"replicas": 0}})
    unsafe = compensation.model_copy(update={"original": ScaleReplicasParams(replicas=0)})
    assert (
        decide(denied, **(context | {"compensation": unsafe})).disposition is RiskDisposition.DENY
    )
    drift = current.model_copy(update={"parameters": ScaleReplicasParams(replicas=3)})
    assert decide(proposal, **(context | {"current": drift})).disposition is RiskDisposition.DENY
    assert (
        decide(proposal, **(context | {"auto_enabled": False})).disposition is RiskDisposition.DENY
    )


@pytest.mark.parametrize(
    "bad_parameters",
    [
        {"container": "server", "limit_memory": "1Mi", "compensate": True},
        {"container": "server", "limit_memory": "1Mi", "request_memory": None},
        {"container": "server"},
        {"container": "server", "limit_memory": 123},
    ],
)
def test_parameters_are_strict_and_cannot_carry_internal_metadata(bad_parameters):
    with pytest.raises(ValueError):
        parse_action_parameters(ActionKind.ADJUST_RESOURCES, bad_parameters)


def test_config_parameter_requires_exact_source_selector():
    with pytest.raises(ValueError):
        parse_action_parameters(
            ActionKind.UPDATE_CONFIG,
            {"source": "env", "config_map": "fixture", "key": "mode", "value": "healthy"},
        )


@pytest.mark.parametrize("problem", ["duplicate", "inverted", "sensitive", "rule-conflict"])
def test_invalid_yaml_closes_policy(tmp_path, problem):
    configured = tmp_path / "remediation"
    copytree(CONFIG, configured)
    services = configured / "services"
    currency = services / "currencyservice.yaml"
    config = services / "config-fixture.yaml"
    if problem == "duplicate":
        (services / "duplicate.yaml").write_bytes(currency.read_bytes())
    elif problem == "inverted":
        config_data = yaml.safe_load(currency.read_text(encoding="utf-8"))
        config_data["resources"]["server"]["limit_cpu"]["hard_max"] = "10m"
        currency.write_text(yaml.safe_dump(config_data), encoding="utf-8")
    elif problem == "sensitive":
        config.write_text(
            config.read_text(encoding="utf-8").replace("VIGIL_MODE", "DB_PASSWORD"),
            encoding="utf-8",
        )
    else:
        config_data = yaml.safe_load(currency.read_text(encoding="utf-8"))
        config_data["rules"].append(config_data["rules"][0] | {"id": "conflicting-rule"})
        currency.write_text(yaml.safe_dump(config_data), encoding="utf-8")
    policy = load_policy(configured)

    assert policy.error
    assert not policy.services
