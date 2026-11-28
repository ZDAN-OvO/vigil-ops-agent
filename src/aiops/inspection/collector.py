"""把已登记的只读检查转换成可判定的观察。"""

from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from math import isfinite
from pathlib import Path

from aiops.contracts import TimeRange, ToolResult
from aiops.inspection.config import InspectionSOP, Manifest, Signal, metric_selector
from aiops.inspection.models import Check, Sample
from aiops.tools.base import ToolContext
from aiops.tools.kubernetes import KubernetesRead
from aiops.tools.prometheus import PrometheusClient


def _registrations(
    manifest: Manifest, step: str
) -> Iterator[tuple[dict[str, str], int, Signal | None]]:
    if step in ("workload-readiness", "workload-signal"):
        for workload in manifest.workloads:
            target = {"namespace": workload.namespace, "kind": workload.kind, "name": workload.name}
            if step == "workload-readiness":
                yield target, workload.readiness_max_age_seconds, None
            else:
                yield target, workload.signal.max_age_seconds, workload.signal
    elif step == "node-ready":
        for node in manifest.nodes:
            yield {"kind": "Node", "name": node.name}, node.max_age_seconds, None
    elif step == "entry-probe":
        for entry in manifest.entries:
            yield {"kind": "Entry", "probe_id": entry.probe_id}, entry.max_age_seconds, None


def plan_checks(manifest: Manifest, sop: InspectionSOP) -> tuple[Check, ...]:
    ordered = (
        (step, target, age, signal)
        for step in sop.steps
        for target, age, signal in _registrations(manifest, step)
    )
    return tuple(
        Check(ordinal=ordinal, check_id=step, target=target, max_age_seconds=age, signal=signal)
        for ordinal, (step, target, age, signal) in enumerate(ordered)
    )


def _failure(result: ToolResult) -> Sample | None:
    if result.error is not None:
        return Sample.insufficient(
            "timeout" if result.error.code == "timeout" else "collection_failed",
            error_code=result.error.code,
        )
    if result.truncated:
        return Sample.insufficient("truncated", raw_ref=result.raw_ref)
    return None


def _reading(value: int | float, timestamp: datetime, healthy: bool, **evidence) -> Sample:
    return Sample(
        "healthy" if healthy else "abnormal",
        None if healthy else "threshold_not_met",
        timestamp,
        {"value": value, **evidence},
    )


class InspectionCollector:
    def __init__(
        self,
        kubernetes: KubernetesRead,
        prometheus: PrometheusClient,
        *,
        cluster: str,
        runtime_path: Path,
        tool_timeout_seconds: float,
        now: Callable[[], Awaitable[datetime]],
    ) -> None:
        self._kubernetes = kubernetes
        self._prometheus = prometheus
        self._cluster = cluster
        self._runtime_path = runtime_path
        self._tool_timeout_seconds = tool_timeout_seconds
        self._now = now

    async def sample(self, check: Check, run_id: str, remaining_seconds: float) -> Sample:
        started = await self._now()
        context = ToolContext.create(
            investigation_id=run_id,
            cluster=self._cluster,
            namespace=check.target.get("namespace", ""),
            runtime_path=self._runtime_path,
            timeout_seconds=min(remaining_seconds, self._tool_timeout_seconds),
        )
        if check.check_id == "workload-readiness":
            return await self._readiness(check, context, started)
        if check.check_id == "workload-signal":
            signal = check.signal
            assert signal is not None
            sample = await self._metric(check, context, started, signal.metric, signal.labels)
            if sample.status == "insufficient":
                return sample
            value = sample.evidence["value"]
            healthy = (signal.minimum is None or value >= signal.minimum) and (
                signal.maximum is None or value <= signal.maximum
            )
            return _reading(
                value,
                sample.sampled_at,
                healthy,
                raw_ref=sample.evidence["raw_ref"],
                minimum=signal.minimum,
                maximum=signal.maximum,
            )
        if check.check_id == "node-ready":
            sample = await self._metric(
                check,
                context,
                started,
                "kube_node_status_condition",
                {
                    "job": "kube-state-metrics",
                    "node": check.target["name"],
                    "condition": "Ready",
                    "status": "true",
                },
            )
        elif check.check_id == "entry-probe":
            labels = {"job": "vigil-entry-probe", "instance": check.target["probe_id"]}
            up = await self._metric(check, context, started, "up", labels)
            if up.status == "insufficient":
                return up
            if up.evidence["value"] != 1:
                return Sample.insufficient("probe_scrape_unavailable", up.sampled_at, **up.evidence)
            sample = await self._metric(check, context, started, "probe_success", labels)
            if sample.status != "insufficient" and sample.sampled_at != up.sampled_at:
                return Sample.insufficient(
                    "probe_sample_mismatch", sample.sampled_at, **sample.evidence
                )
            sample.evidence["scrape_ref"] = up.evidence["raw_ref"]
        else:
            raise ValueError("巡检检查尚未登记")

        if sample.status == "insufficient":
            return sample
        value = sample.evidence["value"]
        if value not in (0, 1):
            return Sample.insufficient("invalid_sample", sample.sampled_at, **sample.evidence)
        return _reading(
            value,
            sample.sampled_at,
            value == 1,
            **{key: val for key, val in sample.evidence.items() if key != "value"},
        )

    async def _readiness(self, check: Check, context: ToolContext, started: datetime) -> Sample:
        result = await self._kubernetes.read_workload_readiness(
            context,
            TimeRange(start=started - timedelta(seconds=1), end=started),
            kind=check.target["kind"],
            name=check.target["name"],
        )
        if failure := _failure(result):
            return failure
        items = result.data["items"]
        if not items:
            return Sample.insufficient("missing_data", raw_ref=result.raw_ref)
        item = items[0]
        evidence = {"raw_ref": result.raw_ref, **item}
        desired = item["desired_replicas"]
        ready = item["ready_replicas"]
        generation = item["generation"]
        observed = item["observed_generation"]
        if None in (desired, ready, generation, observed) or not item["uid"]:
            return Sample.insufficient("missing_data", **evidence)
        if observed < generation:
            return Sample.insufficient("stale_generation", started, **evidence)
        if (await self._now() - started).total_seconds() > check.max_age_seconds:
            return Sample.insufficient("stale_sample", started, **evidence)
        return _reading(ready, started, ready >= desired, **evidence)

    async def _metric(
        self, check: Check, context: ToolContext, at: datetime, metric: str, labels: dict[str, str]
    ) -> Sample:
        # 多保留一个新鲜度窗口，区分可见的过期样本和完全无数据。
        window = 2 * check.max_age_seconds
        result = await self._prometheus.query_samples(
            context,
            metric_selector(metric, labels),
            TimeRange(start=at - timedelta(seconds=window), end=at),
            window_seconds=window,
        )
        if failure := _failure(result):
            return failure
        evidence = {"raw_ref": result.raw_ref}
        series = result.data["series"]
        if result.data["result_type"] != "matrix":
            return Sample.insufficient("invalid_sample", **evidence)
        # 单个登记目标只接收一条精确匹配的指标序列，避免任意选择重复身份。
        if len(series) > 1:
            return Sample.insufficient("ambiguous_data", **evidence)
        if not series or not series[0]["points"]:
            return Sample.insufficient("missing_data", **evidence)
        epoch, value = series[0]["points"][-1]
        try:
            timestamp = datetime.fromtimestamp(epoch, UTC)
        except (OverflowError, OSError, ValueError):
            return Sample.insufficient("invalid_sample", **evidence)
        if timestamp > at or not isinstance(value, (int, float)) or not isfinite(value):
            return Sample.insufficient("invalid_sample", timestamp, **evidence)
        if (await self._now() - timestamp).total_seconds() > check.max_age_seconds:
            return Sample.insufficient("stale_sample", timestamp, value=value, **evidence)
        return _reading(value, timestamp, True, **evidence)
