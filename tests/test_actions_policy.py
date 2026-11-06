"""动作策略：审批、拒绝与自动允许的边界。"""

import pytest
from actions_support import (
    DSN,
    experiment_restart_proposal,
    make_proposal,
    restart_proposal,
)

from aiops.actions.executor import (
    _initial_status,
    proposal_hash,
)
from aiops.actions.policy import decide
from aiops.contracts import (
    ActionOrigin,
    ActionTarget,
    ProposalStatus,
    RiskDecision,
    RiskDisposition,
)

pytestmark = pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL，跳过真实库测试")


def test_policy_requires_approval_for_lab_rollback():
    decision = decide(make_proposal(), target_busy=False)

    assert decision.disposition is RiskDisposition.APPROVAL
    assert decision.policy_version


def test_policy_denies_outside_allowed_namespace():
    proposal = make_proposal(
        target=ActionTarget(
            cluster="vigil-lab",
            namespace="kube-system",
            kind="Deployment",
            name="coredns",
            uid="uid-2",
            resource_version="1",
        )
    )
    decision = decide(proposal, target_busy=False)

    assert decision.disposition is RiskDisposition.DENY
    assert any("白名单" in reason for reason in decision.reasons)


def test_policy_denies_busy_target():
    busy = decide(make_proposal(), target_busy=True)

    assert busy.disposition is RiskDisposition.DENY
    assert any("未完成的执行" in reason for reason in busy.reasons)


def test_model_proposal_cannot_be_created_without_evidence():
    with pytest.raises(ValueError):
        make_proposal(origin=ActionOrigin.MODEL)


def test_proposal_hash_changes_with_target():
    first = make_proposal()
    second = make_proposal(parameters={"from_revision": 3, "to_revision": 1})

    assert proposal_hash(first) == proposal_hash(make_proposal())
    assert proposal_hash(first) != proposal_hash(second)


def test_restart_policy_allows_only_controlled_discardable_pod():
    decision = decide(restart_proposal(), target_busy=False)

    assert decision.disposition is RiskDisposition.APPROVAL


def test_restart_in_experiment_namespace_is_auto_allowed():
    decision = decide(experiment_restart_proposal(), target_busy=False)

    assert decision.disposition is RiskDisposition.AUTO
    assert any("自动执行规则" in reason for reason in decision.reasons)


@pytest.mark.parametrize(
    "override",
    [
        {"discardable": False},
        {"owner_kind": None},
        {"service": ""},
    ],
)
def test_experiment_namespace_auto_still_obeys_hard_constraints(override):
    proposal = experiment_restart_proposal()
    proposal = proposal.model_copy(update={"parameters": {**proposal.parameters, **override}})

    decision = decide(proposal, target_busy=False)

    assert decision.disposition is RiskDisposition.DENY


def test_experiment_namespace_busy_target_is_denied():
    decision = decide(experiment_restart_proposal(), target_busy=True)

    assert decision.disposition is RiskDisposition.DENY


def test_auto_decision_maps_to_approved_proposal_status():
    decision = RiskDecision(
        proposal_id="act-1",
        disposition=RiskDisposition.AUTO,
        policy_version="test",
        reasons=["规则"],
        requires_compensation=False,
    )

    assert _initial_status(decision) is ProposalStatus.APPROVED
    assert _initial_status(decision.model_copy(update={"disposition": RiskDisposition.DENY})) is (
        ProposalStatus.DENIED
    )
    assert (
        _initial_status(decision.model_copy(update={"disposition": RiskDisposition.APPROVAL}))
        is ProposalStatus.PENDING_APPROVAL
    )


def test_auto_restart_stops_after_a_failed_auto_execution():
    decision = decide(experiment_restart_proposal(), target_busy=False, prior_auto_failure=True)

    assert decision.disposition is RiskDisposition.APPROVAL
    assert any("上一次执行失败" in reason for reason in decision.reasons)


def test_rollback_in_experiment_namespace_still_needs_approval():
    proposal = make_proposal(
        target=ActionTarget(
            cluster="vigil-lab",
            namespace="vigil-experiment",
            kind="Deployment",
            name="rebuild-demo",
            uid="uid-3",
            resource_version="1",
        )
    )

    assert decide(proposal, target_busy=False).disposition is RiskDisposition.APPROVAL


def test_restart_outside_experiment_namespace_is_not_auto():
    proposal = restart_proposal()

    assert decide(proposal, target_busy=False).disposition is RiskDisposition.APPROVAL


def test_restart_policy_denies_pod_without_controller():
    proposal = restart_proposal()
    proposal = proposal.model_copy(
        update={"parameters": {**proposal.parameters, "owner_kind": None}}
    )

    decision = decide(proposal, target_busy=False)

    assert decision.disposition is RiskDisposition.DENY
    assert any("ReplicaSet" in reason for reason in decision.reasons)


def test_restart_policy_denies_pod_not_marked_discardable():
    proposal = restart_proposal()
    proposal = proposal.model_copy(
        update={"parameters": {**proposal.parameters, "discardable": False}}
    )

    assert decide(proposal, target_busy=False).disposition is RiskDisposition.DENY


def test_restart_policy_denies_missing_business_service():
    proposal = restart_proposal()
    proposal = proposal.model_copy(update={"parameters": {**proposal.parameters, "service": ""}})

    assert decide(proposal, target_busy=False).disposition is RiskDisposition.DENY
