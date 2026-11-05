"""把动作前后的业务样本归成改善、无改善、恶化或未知。

动作前的基线是判断“有没有变差”的唯一依据。样本不足或资源检查未通过时
不能把结果算作恢复，只能记 unknown。
"""

from __future__ import annotations

from dataclasses import dataclass

from aiops.contracts import AttemptOutcome

WORSE_MARGIN = 0.5


@dataclass(frozen=True)
class SampleSummary:
    samples: int
    failed: int

    @property
    def failure_ratio(self) -> float | None:
        if self.samples <= 0:
            return None
        return self.failed / self.samples

    def to_payload(self) -> dict[str, float | int | None]:
        return {
            "samples": self.samples,
            "failed_samples": self.failed,
            "failure_ratio": self.failure_ratio,
        }


def _crosses_margin(baseline: SampleSummary, post: SampleSummary) -> bool:
    """用整数交叉相乘判断失败率差是否达到 0.5，避免浮点相减漏判。"""
    return (
        2 * (post.failed * baseline.samples - baseline.failed * post.samples)
        >= post.samples * baseline.samples
    )


@dataclass(frozen=True)
class OutcomeDecision:
    outcome: AttemptOutcome
    reason: str
    baseline: SampleSummary
    post: SampleSummary


def classify_outcome(
    baseline: SampleSummary,
    post: SampleSummary,
    *,
    business_ok: bool,
    resource_ok: bool,
    known_failure: bool = False,
    post_reliable: bool = True,
    unavailable_reason: str = "",
) -> OutcomeDecision:
    """按固定规则分类；任何无法比较的情况都归 unknown。"""
    if unavailable_reason:
        return OutcomeDecision(AttemptOutcome.UNKNOWN, unavailable_reason, baseline, post)
    before = baseline.failure_ratio
    if before is None:
        return OutcomeDecision(
            AttemptOutcome.UNKNOWN, "动作前基线样本不足，无法判断", baseline, post
        )
    if not post_reliable:
        return OutcomeDecision(AttemptOutcome.UNKNOWN, "动作后样本不足，无法判断", baseline, post)
    after = post.failure_ratio
    if after is None:
        if not known_failure:
            return OutcomeDecision(
                AttemptOutcome.UNKNOWN, "动作后样本不足，无法判断", baseline, post
            )
        # 已确认执行失败但没有可采样的业务结果，按全失败比较。
        after = 1.0
    if business_ok and resource_ok:
        return OutcomeDecision(
            AttemptOutcome.IMPROVED, "动作后业务与资源检查均通过", baseline, post
        )
    if business_ok and not resource_ok:
        return OutcomeDecision(
            AttemptOutcome.UNKNOWN, "业务请求恢复，但资源检查未通过", baseline, post
        )
    if before == 0.0 and after > 0.0:
        return OutcomeDecision(
            AttemptOutcome.WORSENED, "动作前无失败，动作后出现失败", baseline, post
        )
    if post.samples == 0:
        # 已确认失败但没有后验样本时，用“全失败”样本参与同一个 0.5 规则。
        full_failure = SampleSummary(samples=1, failed=1)
        if _crosses_margin(baseline, full_failure):
            return OutcomeDecision(
                AttemptOutcome.WORSENED,
                "动作后没有有效样本，按全失败比较且超过恶化阈值",
                baseline,
                post,
            )
        return OutcomeDecision(
            AttemptOutcome.NO_IMPROVEMENT,
            "动作后没有有效样本，按全失败比较仍未达到恶化阈值",
            baseline,
            post,
        )
    if _crosses_margin(baseline, post):
        return OutcomeDecision(
            AttemptOutcome.WORSENED,
            f"动作后失败率比基线高 {after - before:.2f}，达到 {WORSE_MARGIN:.2f} 阈值",
            baseline,
            post,
        )
    return OutcomeDecision(
        AttemptOutcome.NO_IMPROVEMENT, "动作后仍有失败，失败率没有明显下降", baseline, post
    )
