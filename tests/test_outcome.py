"""动作结果分类的固定规则；阈值不随场景改变。"""

from aiops.actions.outcome import SampleSummary, classify_outcome
from aiops.contracts import AttemptOutcome


def decide(before: tuple[int, int], after: tuple[int, int], **overrides):
    return classify_outcome(
        SampleSummary(samples=before[0], failed=before[1]),
        SampleSummary(samples=after[0], failed=after[1]),
        **overrides,
    )


def test_healthy_result_is_improved():
    decision = decide((3, 0), (6, 0), business_ok=True, resource_ok=True)

    assert decision.outcome is AttemptOutcome.IMPROVED


def test_same_failure_level_is_no_improvement():
    decision = decide((3, 3), (6, 6), business_ok=False, resource_ok=True)

    assert decision.outcome is AttemptOutcome.NO_IMPROVEMENT


def test_new_failure_from_a_healthy_baseline_is_worsened():
    decision = decide((3, 0), (6, 6), business_ok=False, resource_ok=True)

    assert decision.outcome is AttemptOutcome.WORSENED
    assert "动作前无失败" in decision.reason


def test_failure_rate_increase_at_the_margin_is_worsened():
    decision = decide((4, 1), (4, 3), business_ok=False, resource_ok=True)

    assert decision.outcome is AttemptOutcome.WORSENED


def test_tenths_boundary_is_worsened_without_floating_error():
    decision = decide((10, 2), (10, 7), business_ok=False, resource_ok=True)

    assert decision.outcome is AttemptOutcome.WORSENED


def test_healthy_tail_after_warmup_failures_is_improved():
    decision = decide((3, 0), (3, 0), business_ok=True, resource_ok=True)

    assert decision.outcome is AttemptOutcome.IMPROVED


def test_tail_failure_from_a_healthy_baseline_is_worsened():
    decision = decide((3, 0), (3, 1), business_ok=False, resource_ok=True)

    assert decision.outcome is AttemptOutcome.WORSENED


def test_failure_rate_increase_below_the_margin_is_no_improvement():
    decision = decide((100, 40), (100, 89), business_ok=False, resource_ok=True)

    assert decision.outcome is AttemptOutcome.NO_IMPROVEMENT


def test_missing_baseline_is_unknown():
    decision = decide((0, 0), (6, 6), business_ok=False, resource_ok=True)

    assert decision.outcome is AttemptOutcome.UNKNOWN


def test_missing_post_samples_is_unknown():
    decision = decide((3, 3), (0, 0), business_ok=False, resource_ok=True)

    assert decision.outcome is AttemptOutcome.UNKNOWN


def test_unreliable_post_samples_are_unknown_even_without_failures():
    decision = decide((3, 0), (1, 0), business_ok=False, resource_ok=True, post_reliable=False)

    assert decision.outcome is AttemptOutcome.UNKNOWN


def test_known_failure_without_samples_uses_full_failure():
    decision = decide((3, 0), (0, 0), business_ok=False, resource_ok=False, known_failure=True)

    assert decision.outcome is AttemptOutcome.WORSENED


def test_known_failure_with_a_failing_baseline_is_no_improvement():
    decision = decide((3, 3), (0, 0), business_ok=False, resource_ok=False, known_failure=True)

    assert decision.outcome is AttemptOutcome.NO_IMPROVEMENT


def test_known_failure_with_a_partially_failing_baseline_can_worsen():
    decision = decide((5, 2), (0, 0), business_ok=False, resource_ok=False, known_failure=True)

    assert decision.outcome is AttemptOutcome.WORSENED


def test_business_recovered_but_resource_check_failed_is_unknown():
    decision = decide((3, 3), (6, 0), business_ok=True, resource_ok=False)

    assert decision.outcome is AttemptOutcome.UNKNOWN
    assert "资源检查" in decision.reason


def test_explicit_unavailable_reason_wins():
    decision = decide(
        (3, 0), (6, 0), business_ok=True, resource_ok=True, unavailable_reason="适配器缺失"
    )

    assert decision.outcome is AttemptOutcome.UNKNOWN
    assert decision.reason == "适配器缺失"
