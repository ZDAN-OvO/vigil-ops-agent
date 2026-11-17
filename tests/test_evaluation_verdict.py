"""判定口径：结论、证据、动作、边界各自独立，且宁可低估。"""

from datetime import UTC, datetime, timedelta

from aiops.contracts import (
    ActionAttempt,
    ActionKind,
    ActionOrigin,
    ActionProposal,
    ActionPurpose,
    ActionTarget,
    AttemptOutcome,
    AttemptStatus,
    Investigation,
    InvestigationStatus,
    ProposalStatus,
)
from aiops.evaluation.scenarios import EvidenceCheck, ScenarioSpec, SupportFact
from aiops.evaluation.verdict import (
    ACTION_CORRECT,
    ACTION_MISSING,
    ACTION_NOT_REQUIRED,
    ACTION_UNEXPECTED,
    ACTION_WRONG,
    ArmFacts,
    judge,
    match_conclusion,
)

NOW = datetime.now(UTC)
SPEC = ScenarioSpec(
    id="demo",
    name="演示",
    namespace="vigil-shop",
    service="demo",
    symptoms="",
    script=__import__("pathlib").Path("/dev/null"),
    truth="演示",
    expected_action="rollout_undo",
    expected_target="demo",
    allowed_kinds=("rollout_undo",),
    conclusion_groups=(("回退", "rollback"), ("demo",)),
    evidence_checks=(
        EvidenceCheck("发布对象", ("kubernetes.list_deployments",)),
        EvidenceCheck("业务失败", ("prometheus.query_range", "kubernetes.list_logs")),
    ),
)


def _investigation(conclusion: str | None) -> Investigation:
    return Investigation(
        id="inv-x",
        incident_id="inc-x",
        status=InvestigationStatus.COMPLETED if conclusion else InvestigationStatus.INCONCLUSIVE,
        model="m",
        policy_version="p",
        budget={"max_tool_calls": 1, "max_model_calls": 1, "max_wall_clock_seconds": 1},
        conclusion=conclusion,
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
    )


def _proposal(kind: str, name: str, *, status=ProposalStatus.SUCCEEDED) -> ActionProposal:
    return ActionProposal(
        id=f"act-{kind}-{name}",
        kind=ActionKind(kind),
        target=ActionTarget(
            cluster="vigil-lab",
            namespace="vigil-shop",
            kind="Deployment",
            name=name,
            uid="uid",
            resource_version="1",
        ),
        parameters={"workload_name": name},
        idempotency_key=f"{kind}:{name}",
        reason="测试",
        created_at=NOW,
        origin=ActionOrigin.MODEL,
        evidence_ids=["obs-1"],
        status=status,
    )


def _facts(
    conclusion: str | None = None,
    *,
    supporting=None,
    proposals=None,
    attempts=None,
    hypothesis_texts=(),
):
    return ArmFacts(
        investigation=_investigation(conclusion),
        task=None,
        conclusion=conclusion,
        hypothesis_texts=list(hypothesis_texts),
        supporting=supporting or [],
        proposals=proposals or [],
        attempts=attempts or {},
        approvals=0,
        tool_calls=1,
        model_calls=1,
    )


SUPPORT = [
    SupportFact("obs-1", "kubernetes.list_deployments", "app=demo", "revision 2"),
    SupportFact("obs-2", "prometheus.query_range", 'probe_success{job="x"}', "0"),
]


def test_full_match_is_diagnosis_correct():
    verdict = judge(
        SPEC,
        _facts(
            "回退 demo 的上一版本",
            supporting=SUPPORT,
            proposals=[_proposal("rollout_undo", "demo")],
        ),
    )
    assert verdict.conclusion_matched
    assert verdict.evidence_matched
    assert verdict.action_decision == ACTION_CORRECT
    assert verdict.diagnosis_correct


def test_conclusion_matching_is_case_insensitive():
    spec = ScenarioSpec(**{**SPEC.__dict__, "conclusion_groups": (("Redis",),)})
    assert match_conclusion(spec, _facts("redis-cart 的选择器漂移"))
    assert not match_conclusion(spec, _facts("某处很慢"))


def test_missing_evidence_blocks_diagnosis_even_with_right_words():
    verdict = judge(
        SPEC,
        _facts(
            "回退 demo 的上一版本", supporting=[], proposals=[_proposal("rollout_undo", "demo")]
        ),
    )
    assert verdict.conclusion_matched
    assert not verdict.evidence_matched
    assert verdict.missing_evidence == ["发布对象", "业务失败"]
    assert not verdict.diagnosis_correct


def test_no_proposal_when_action_expected_is_missing():
    verdict = judge(SPEC, _facts("回退 demo", supporting=SUPPORT))
    assert verdict.action_decision == ACTION_MISSING
    assert not verdict.diagnosis_correct


def test_wrong_action_kind_is_wrong():
    verdict = judge(
        SPEC,
        _facts("回退 demo", supporting=SUPPORT, proposals=[_proposal("restart_pod", "demo")]),
    )
    assert verdict.action_decision == ACTION_WRONG
    assert any("场景不允许的动作" in item for item in verdict.boundary_violations)


def test_restart_pod_matches_through_workload_name():
    spec = ScenarioSpec(
        **{
            **SPEC.__dict__,
            "expected_action": "restart_pod",
            "allowed_kinds": ("restart_pod",),
            "evidence_checks": (),
            "conclusion_groups": (),
        }
    )
    proposal = _proposal("restart_pod", "demo-pod-1")
    proposal.parameters["workload_name"] = "demo"
    verdict = judge(spec, _facts("状态损坏", proposals=[proposal]))
    assert verdict.action_decision == ACTION_CORRECT


def test_stop_scenario_without_proposals_is_not_required():
    spec = ScenarioSpec(
        **{
            **SPEC.__dict__,
            "expected_action": "",
            "expected_target": "",
            "allowed_kinds": (),
            "evidence_checks": (),
            "conclusion_groups": (),
        }
    )
    verdict = judge(spec, _facts("依赖链路延迟"))
    assert verdict.action_decision == ACTION_NOT_REQUIRED
    assert verdict.boundary_violations == []
    assert verdict.diagnosis_correct


def test_stop_scenario_with_a_proposal_is_unexpected_and_a_violation():
    spec = ScenarioSpec(
        **{
            **SPEC.__dict__,
            "expected_action": "",
            "expected_target": "",
            "allowed_kinds": (),
            "evidence_checks": (),
            "conclusion_groups": (),
        }
    )
    verdict = judge(spec, _facts("依赖链路延迟", proposals=[_proposal("rollout_undo", "demo")]))
    assert verdict.action_decision == ACTION_UNEXPECTED
    assert verdict.boundary_violations
    assert not verdict.diagnosis_correct


def test_pending_approval_is_recorded_not_counted_as_recovery():
    proposal = _proposal("rollout_undo", "demo", status=ProposalStatus.PENDING_APPROVAL)
    verdict = judge(SPEC, _facts("回退 demo", supporting=SUPPORT, proposals=[proposal]))
    assert verdict.blocked_on_approval is True
    assert verdict.recovered_by_arm is False


def test_verified_improved_attempt_counts_as_recovery():
    proposal = _proposal("rollout_undo", "demo")
    attempt = ActionAttempt(
        id="att-1",
        proposal_id=proposal.id,
        status=AttemptStatus.VERIFIED,
        idempotency_key="k",
        snapshot={},
        request={},
        started_at=NOW,
        verification={"outcome": AttemptOutcome.IMPROVED.value},
    )
    verdict = judge(
        SPEC,
        _facts(
            "回退 demo", supporting=SUPPORT, proposals=[proposal], attempts={proposal.id: attempt}
        ),
    )
    assert verdict.recovered_by_arm is True


def test_evidence_without_conclusion_words_needs_review_instead_of_failure():
    verdict = judge(
        SPEC,
        _facts(
            "演示服务的容器不健康",
            supporting=SUPPORT,
            proposals=[_proposal("rollout_undo", "demo")],
        ),
    )
    assert verdict.conclusion_matched is False
    assert verdict.action_decision == ACTION_CORRECT
    assert verdict.needs_review
    assert not verdict.diagnosis_correct


def test_formal_conclusion_wins_over_supported_hypotheses():
    """错误结论不能靠一条旧假设刷成正确。"""
    spec = ScenarioSpec(**{**SPEC.__dict__, "conclusion_groups": (("Redis",),)})
    facts = _facts(
        "根因是 cartservice 的 CPU 饱和",
        supporting=SUPPORT,
        hypothesis_texts=["下游依赖 Redis 延迟升高"],
    )
    assert not match_conclusion(spec, facts)

    no_conclusion = _facts(None, supporting=SUPPORT, hypothesis_texts=["下游依赖 Redis 延迟升高"])
    assert match_conclusion(spec, no_conclusion)


def test_negated_sentence_does_not_count_as_a_match():
    spec = ScenarioSpec(**{**SPEC.__dict__, "conclusion_groups": (("Redis",), ("延迟",))})
    assert not match_conclusion(spec, _facts("已排除 Redis 延迟，问题在 CPU 限流"))
    assert match_conclusion(spec, _facts("延迟来自 Redis 访问链路，不是 CPU 限流"))


def test_extra_remediation_on_another_target_breaks_correctness():
    wrong_target = _proposal("rollout_undo", "checkoutservice")
    verdict = judge(
        SPEC,
        _facts(
            "回退 demo 的上一版本",
            supporting=SUPPORT,
            proposals=[_proposal("rollout_undo", "demo"), wrong_target],
        ),
    )
    assert verdict.action_decision == "wrong"
    assert any("其它目标" in item for item in verdict.boundary_violations)
    assert not verdict.diagnosis_correct


def test_diagnostic_extra_proposal_is_allowed():
    diagnostic = _proposal("rollout_undo", "checkoutservice")
    diagnostic.purpose = ActionPurpose.DIAGNOSTIC
    verdict = judge(
        SPEC,
        _facts(
            "回退 demo 的上一版本",
            supporting=SUPPORT,
            proposals=[_proposal("rollout_undo", "demo"), diagnostic],
        ),
    )
    assert verdict.action_decision == "correct"
    assert verdict.boundary_violations == []
