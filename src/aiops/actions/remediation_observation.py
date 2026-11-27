"""用 Prometheus 原始时间戳核对业务观察窗和独立故障信号。"""

import json
import re
from asyncio import gather
from bisect import bisect_left, bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Any, Literal

import httpx

from aiops.actions.outcome import SampleSummary, classify_outcome
from aiops.actions.remediation_kube import ObservedPod
from aiops.contracts import AttemptOutcome

_SCRAPE_SECONDS = 15
_MAX_SIGNAL_GAP_SECONDS = 2 * _SCRAPE_SECONDS
_CPU_PERIODS = "container_cpu_cfs_periods_total"
_CPU_THROTTLED = "container_cpu_cfs_throttled_periods_total"
_RESTARTS = "kube_pod_container_status_restarts_total"


class RemediationMetrics:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def _single(self, query: str, at: datetime, expected_type: str) -> dict | None:
        response = await self._client.get(
            "/api/v1/query", params={"query": query, "time": at.timestamp()}
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
            return None
        data = body["data"]
        result = data.get("result")
        if (
            body.get("status") != "success"
            or body.get("warnings")
            or data.get("resultType") != expected_type
            or not isinstance(result, list)
            or len(result) != 1
        ):
            return None
        return result[0] if isinstance(result[0], dict) else None

    async def scalar(self, query: str, at: datetime) -> float | None:
        result = await self._single(query, at, "vector")
        if result is None:
            return None
        point = _point(result.get("value"))
        return point[1] if point is not None else None

    async def fresh(
        self,
        selector: str,
        at: datetime,
        *,
        max_age: int,
    ) -> tuple[float, float] | None:
        # Instant 回包的时间是求值时刻，timestamp() 才取原始采样时刻。
        value = await self.scalar(selector, at)
        timestamp = await self.scalar(f"timestamp({selector})", at)
        if value is None or timestamp is None or not 0 <= at.timestamp() - timestamp <= max_age:
            return None
        return value, timestamp

    async def raw_samples(
        self,
        selector: str,
        start: datetime,
        end: datetime,
    ) -> list[tuple[float, float]]:
        duration = max(1, int((end - start).total_seconds()) + 2)
        result = await self._single(f"{selector}[{duration}s]", end, "matrix")
        return _samples(result, start, end)


def _point(pair: object) -> tuple[float, float] | None:
    if not isinstance(pair, list) or len(pair) != 2:
        return None
    try:
        timestamp, value = map(float, pair)
    except (TypeError, ValueError, OverflowError):
        return None
    return (timestamp, value) if isfinite(timestamp) and isfinite(value) else None


def _samples(result: dict | None, start: datetime, end: datetime) -> list[tuple[float, float]]:
    values = result.get("values") if result is not None else None
    if not isinstance(values, list):
        return []
    samples: dict[float, float] = {}
    for pair in values:
        point = _point(pair)
        if point is None:
            return []
        timestamp, value = point
        if start.timestamp() <= timestamp < end.timestamp():
            if timestamp in samples and samples[timestamp] != value:
                return []
            samples[timestamp] = value
    return sorted(samples.items())


@dataclass(frozen=True)
class FaultSignal:
    key: str
    status: Literal["clear", "recurred", "unknown"]
    evidence: dict[str, Any]

    def payload(self) -> dict[str, Any]:
        return {"signal": self.key, "status": self.status, **self.evidence}


def _covered(points: list[tuple[float, float]], start: datetime, end: datetime) -> bool:
    timestamps = [start.timestamp(), *(ts for ts, _ in points), end.timestamp()]
    return bool(points) and all(
        0 <= right - left <= _MAX_SIGNAL_GAP_SECONDS
        for left, right in zip(timestamps, timestamps[1:], strict=False)
    )


def _check_signal(
    key: str,
    points: list[tuple[float, float]],
    start: datetime,
    end: datetime,
    *,
    valid: Callable[[float], bool],
    adverse: Callable[[float], bool],
    evidence: dict[str, Any],
) -> FaultSignal:
    proof = {**evidence, "samples": [list(point) for point in points]}
    if any(valid(value) and adverse(value) for _, value in points):
        return FaultSignal(key, "recurred", proof)
    if not _covered(points, start, end) or any(not valid(value) for _, value in points):
        return FaultSignal(key, "unknown", proof)
    return FaultSignal(key, "clear", proof)


async def check_history(
    metrics: RemediationMetrics,
    key: str,
    selector: str,
    start: datetime,
    end: datetime,
    *,
    adverse: Callable[[float], bool],
    valid: Callable[[float], bool] = isfinite,
) -> FaultSignal:
    return _check_signal(
        key,
        await metrics.raw_samples(selector, start, end),
        start,
        end,
        valid=valid,
        adverse=adverse,
        evidence={"selector": selector},
    )


async def check_config_loaded(
    metrics: RemediationMetrics,
    selector: str,
    start: datetime,
    end: datetime,
) -> FaultSignal:
    evidence = {"selector": selector}
    # 期望模式的指标在应用改为其他模式时会消失。以同一导出器的
    # up 抓取时刻为准，避免一次消失后恢复的序列被正常采样间隔掩盖。
    matchers = re.findall(r'(?:[{,])\s*((?:job|instance)="[^"\\]+")\s*(?=[,}])', selector)
    if not any(matcher.startswith("job=") for matcher in matchers):
        return FaultSignal("config_loaded", "unknown", evidence)
    up = f"up{{{','.join(matchers)}}}"
    loaded, scrapes = await gather(
        metrics.raw_samples(selector, start, end),
        metrics.raw_samples(up, start, end),
    )
    known = dict(loaded)
    points = [(ts, known.get(ts, -1.0) if reachable == 1 else -1.0) for ts, reachable in scrapes]
    return _check_signal(
        "config_loaded",
        points,
        start,
        end,
        valid=lambda value: value in (0, 1),
        adverse=lambda value: value == 0,
        evidence=evidence | {"exporter": up},
    )


async def check_replica_capacity(
    metrics: RemediationMetrics,
    rejected_selector: str,
    endpoints_selector: str,
    replicas: int,
    start: datetime,
    end: datetime,
) -> list[FaultSignal]:
    # 导出器在每次抓取时可能重复上一次交易。按实际交易时间
    # 去重，也排除观察窗开始前的容量测试。
    rejected_metric, _, matchers = rejected_selector.partition("{")
    if rejected_metric != "vigil_replica_fixture_rejected" or not matchers:
        return [FaultSignal("replica_capacity", "unknown", {"selector": rejected_selector})]
    clock = "vigil_replica_fixture_batch_timestamp_seconds{" + matchers
    rejected, endpoints, scrapes = await gather(
        metrics.raw_samples(rejected_selector, start, end),
        metrics.raw_samples(endpoints_selector, start, end),
        metrics.raw_samples(clock, start, end),
    )
    results = []
    for key, selector, samples, adverse in (
        ("rejected_requests", rejected_selector, rejected, lambda value: value > 0),
        ("ready_endpoints", endpoints_selector, endpoints, lambda value: value < replicas),
    ):
        values = dict(samples)
        batches = {}
        for scraped_at, batch_at in scrapes:
            if not start.timestamp() <= batch_at < end.timestamp():
                continue
            value = values.get(scraped_at, -1.0)
            if not 0 <= scraped_at - batch_at <= 45 or batches.get(batch_at, value) != value:
                value = -1.0
            batches[batch_at] = value
        results.append(
            _check_signal(
                key,
                sorted(batches.items()),
                start,
                end,
                valid=lambda value, signal_key=key: (
                    value >= 0
                    and value == int(value)
                    and (signal_key != "ready_endpoints" or value <= replicas)
                ),
                adverse=adverse,
                evidence={
                    "selector": selector,
                    "batch_timestamp_selector": clock,
                    "expected_replicas": replicas,
                },
            )
        )
    return results


def _pod_selector(job: str, namespace: str, container: str, pod: ObservedPod, **labels: str) -> str:
    matchers = {"job": job, "namespace": namespace, "container": container, "pod": pod.name}
    return ",".join(
        f"{name}{'=' if name != 'id' else '=~'}{json.dumps(value)}"
        for name, value in (matchers | labels).items()
    )


async def check_oom(
    metrics: RemediationMetrics,
    namespace: str,
    container: str,
    initial: dict[str, int],
    pods: tuple[ObservedPod, ...],
    start: datetime,
    end: datetime,
    *,
    recurrence_start: datetime,
) -> list[FaultSignal]:
    async def check(pod: ObservedPod) -> FaultSignal:
        evidence = {"pod": pod.name, "pod_uid": pod.uid, "container": container}
        in_window = [at.isoformat() for at in pod.oom_finished_at if recurrence_start <= at < end]
        if in_window:
            return FaultSignal("oom_killed", "recurred", evidence | {"finished_at": in_window})
        if pod.uid not in initial or initial[pod.uid] != pod.restarts:
            return FaultSignal("oom_killed", "unknown", evidence | {"restarts": pod.restarts})
        labels = _pod_selector("kube-state-metrics", namespace, container, pod, uid=pod.uid)
        selector = f"{_RESTARTS}{{{labels}}}"
        return _check_signal(
            "oom_killed",
            await metrics.raw_samples(selector, start, end),
            start,
            end,
            valid=lambda value: value == initial[pod.uid],
            adverse=lambda _: False,
            evidence=evidence | {"selector": selector, "restarts": pod.restarts},
        )

    return list(await gather(*(check(pod) for pod in pods)))


async def check_cpu(
    metrics: RemediationMetrics,
    namespace: str,
    container: str,
    pods: tuple[ObservedPod, ...],
    start: datetime,
    end: datetime,
    *,
    adverse: Callable[[float], bool],
) -> FaultSignal:
    evidence: dict[str, Any] = {"container": container, "pods": [pod.uid for pod in pods]}
    if not pods:
        return FaultSignal("throttle_ratio", "unknown", evidence)
    selectors = []
    for pod in pods:
        _, _, container_id = pod.container_id.partition("://")
        if not re.fullmatch(r"[0-9a-f]{64}", container_id):
            return FaultSignal("throttle_ratio", "unknown", evidence)
        labels = _pod_selector(
            "kubelet-cadvisor", namespace, container, pod, id=rf".*{container_id}(\.scope)?"
        )
        selectors.append((f"{_CPU_PERIODS}{{{labels}}}", f"{_CPU_THROTTLED}{{{labels}}}"))
    samples = await gather(
        *(metrics.raw_samples(selector, start, end) for pair in selectors for selector in pair)
    )
    evidence["counters"] = [
        {"selector": selector, "samples": [list(point) for point in points]}
        for selector, points in zip(
            (selector for pair in selectors for selector in pair), samples, strict=True
        )
    ]
    counters = []
    for periods, throttled in zip(samples[::2], samples[1::2], strict=True):
        stamps = [at for at, _ in periods]
        if (
            len(periods) < 2
            or not _covered(periods, start, end)
            or stamps != [at for at, _ in throttled]
            or any(
                value < 0 or value != int(value)
                for series in (periods, throttled)
                for _, value in series
            )
            or any(
                previous > value
                for series in (periods, throttled)
                for (_, previous), (_, value) in zip(series, series[1:], strict=False)
            )
        ):
            return FaultSignal("throttle_ratio", "unknown", evidence)
        counters.append(
            (stamps, [value for _, value in periods], [value for _, value in throttled])
        )
    # 用当前容器在窗内的计数器计算滑动一分钟的速率。头一分钟只使用
    # 已抓取的窗内样本，避免把调限前的节流归给本次生效。
    evaluations = sorted({stamp for stamps, _, _ in counters for stamp in stamps[1:]})
    first_complete = max(stamps[1] for stamps, _, _ in counters)
    ratios = []
    missing = False
    for at in evaluations:
        total = limited = 0.0
        covered = True
        for stamps, periods, throttled in counters:
            first = bisect_left(stamps, at - 60)
            last = bisect_right(stamps, at) - 1
            if first >= last:
                covered = False
                break
            duration = stamps[last] - stamps[first]
            total += (periods[last] - periods[first]) / duration
            limited += (throttled[last] - throttled[first]) / duration
        if not covered:
            missing |= at >= first_complete
            continue
        if not 0 <= limited <= total or total == 0:
            missing = True
            continue
        ratios.append((at, limited / total))
    proof = evidence | {"samples": [list(point) for point in ratios]}
    if any(adverse(value) for _, value in ratios):
        return FaultSignal("throttle_ratio", "recurred", proof)
    return FaultSignal("throttle_ratio", "unknown" if missing or not ratios else "clear", proof)


def _business_ok(value: float) -> bool | None:
    if value in (0, 1):
        return bool(value)
    if 100 <= value <= 599 and int(value) == value:
        return 200 <= value < 300
    return None


def _business_slots(
    points: list[tuple[float, float]], start: datetime, *, seconds: int, count: int
) -> dict[int, dict[float, bool]]:
    slots: dict[int, dict[float, bool]] = {}
    origin = start.timestamp()
    for timestamp, value in points:
        slot = int((timestamp - origin) // seconds)
        ok = _business_ok(value)
        if 0 <= slot < count and ok is not None:
            slots.setdefault(slot, {})[timestamp] = ok
    return slots


@dataclass(frozen=True)
class WindowSamples:
    start: datetime
    seconds: int
    slots: tuple[tuple[tuple[float, bool], ...], ...]

    @classmethod
    def collect(
        cls,
        points: list[tuple[float, float]],
        start: datetime,
        *,
        seconds: int,
        count: int,
    ) -> "WindowSamples":
        slots = _business_slots(points, start, seconds=seconds, count=count)
        return cls(
            start,
            seconds,
            tuple(tuple(sorted(slots.get(index, {}).items())) for index in range(count)),
        )

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.seconds * len(self.slots))

    def summary(self, *, count: int | None = None) -> SampleSummary:
        # 每时段只记一次业务结果。该时段出现任何失败，就不能记为通过。
        populated = [slot for slot in self.slots[:count] if slot]
        return SampleSummary(
            samples=len(populated),
            failed=sum(not all(ok for _, ok in slot) for slot in populated),
        )

    def payload(self) -> dict[str, Any]:
        summary = self.summary()
        return {
            "method": "business_probe_window",
            "started_at": self.start.isoformat(),
            "slot_seconds": self.seconds,
            "slots_required": len(self.slots),
            "slots_collected": summary.samples,
            "failed_slots": summary.failed,
            "samples": [[list(point) for point in slot] for slot in self.slots],
        }


async def collect_window(
    metrics: RemediationMetrics,
    selector: str,
    start: datetime,
    now: datetime,
    *,
    seconds: int,
    count: int,
) -> WindowSamples:
    end = min(now, start + timedelta(seconds=seconds * count))
    return WindowSamples.collect(
        await metrics.raw_samples(selector, start, end),
        start,
        seconds=seconds,
        count=count,
    )


async def collect_resource_windows(
    metrics: RemediationMetrics,
    selector: str,
    effective_at: datetime,
    now: datetime,
    *,
    seconds: int,
    count: int,
    recovery_deadline: datetime,
    recovered_at: datetime | None,
) -> tuple[WindowSamples, WindowSamples | None]:
    early_end = effective_at + timedelta(seconds=5 * seconds)
    window_end = (
        recovery_deadline
        if recovered_at is None
        else recovered_at + timedelta(seconds=count * seconds)
    )
    points = await metrics.raw_samples(selector, effective_at, min(now, max(early_end, window_end)))
    early = WindowSamples.collect(points, effective_at, seconds=seconds, count=5)
    if recovered_at is None:
        start, deadline = effective_at.timestamp(), recovery_deadline.timestamp()
        first_success = next(
            (at for at, value in points if start <= at < deadline and _business_ok(value)), None
        )
        if first_success is None:
            return early, None
        recovered_at = datetime.fromtimestamp(first_success, UTC)
    return early, WindowSamples.collect(points, recovered_at, seconds=seconds, count=count)


async def latest_healthy_window(
    metrics: RemediationMetrics,
    selector: str,
    start: datetime,
    end: datetime,
    *,
    seconds: int,
    count: int,
) -> WindowSamples | None:
    # 告警可能等满足持续时间才发送；下次发作前可以先有一段完整的恢复窗口。
    total = int((end - start).total_seconds() // seconds)
    if total < count:
        return None
    points = await metrics.raw_samples(selector, start, end)
    # 从新发作时间向前对齐，保留靠近新发作的最近一段完整窗口。
    origin = end - timedelta(seconds=total * seconds)
    slots = _business_slots(points, origin, seconds=seconds, count=total)
    run = 0
    previous = -2
    last_success = None
    for index, samples in sorted(slots.items()):
        if all(samples.values()):
            run = run + 1 if previous == index - 1 else 1
            if run >= count:
                last_success = index
        else:
            run = 0
        previous = index
    if last_success is None:
        return None
    first = last_success + 1 - count
    return WindowSamples(
        origin + timedelta(seconds=first * seconds),
        seconds,
        tuple(tuple(sorted(slots[index].items())) for index in range(first, last_success + 1)),
    )


def worsened(baseline: SampleSummary, post: WindowSamples) -> bool:
    initial = post.summary(count=5)
    if baseline.samples != 5 or initial.samples != 5:
        return False
    return (
        classify_outcome(
            baseline,
            initial,
            business_ok=False,
            resource_ok=True,
        ).outcome
        is AttemptOutcome.WORSENED
    )
