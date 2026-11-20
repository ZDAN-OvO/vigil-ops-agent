"""应用服务调用的六块值班快照。查询范围由启动配置固定。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx

from aiops.config import Settings
from aiops.monitoring.releases import (
    HISTORY_WINDOW,
    PostgresRollbackHistory,
    RollbackHistory,
    RollbackRecord,
    changes,
)
from aiops.monitoring.services import restart_query, service_health
from aiops.monitoring.signals import (
    alert_groups,
    as_time,
    block,
    iso,
    nodes,
    probes,
    prometheus_series,
)
from aiops.tools.kubernetes import KubernetesRead, MonitoringResource, trim_event

DASHBOARDS = {
    "alerts": "vigil-alerts",
    "probes": "vigil-entry-probes",
    "changes": "vigil-releases",
    "services": "vigil-services",
    "nodes": "vigil-nodes",
}


def grafana_links(base: str | None) -> dict[str, str]:
    if not base:
        return {}
    parts = urlsplit(base)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or "\\" in base
        or any(char.isspace() for char in base)
    ):
        raise ValueError("Grafana 地址必须是 HTTP(S) 基础地址")
    return {name: f"{base.rstrip('/')}/d/{uid}" for name, uid in DASHBOARDS.items()}


async def _read[T](call: Awaitable[T], timeout: float) -> T | None:
    try:
        return await asyncio.wait_for(call, timeout=timeout)
    except Exception:
        # 上游异常可能带本机路径、URL 或响应正文。块只公开固定来源名。
        return None


async def _panel(call: Awaitable[dict], source: str, timeout: float) -> dict:
    result = await _read(call, timeout)
    return result if result is not None else block(failed=[source])


def _warnings(items: list[dict] | None, now: datetime) -> dict:
    if items is None:
        return block(failed=["events"])
    start = now - timedelta(hours=1)
    rows = []
    incomplete = False
    for raw in items:
        event = trim_event(raw)
        if event["type"] != "Warning":
            continue
        last_seen = as_time(
            event["last_seen"] or event["first_seen"] or (raw["metadata"].get("creation_timestamp"))
        )
        if last_seen is None:
            incomplete = True
            continue
        if start <= last_seen <= now:
            rows.append(
                {
                    "reason": event["reason"],
                    "object": {
                        "kind": event["involved"]["kind"],
                        "name": event["involved"]["name"],
                    },
                    "message": event["message"],
                    "count": event["count"],
                    "last_seen": iso(last_seen),
                }
            )
    rows.sort(key=lambda row: row["last_seen"], reverse=True)
    return block({"items": rows}, missing=["event_timestamp"] if incomplete else [])


class Monitoring:
    def __init__(
        self,
        kubernetes: KubernetesRead,
        *,
        namespace: str,
        prometheus_url: str,
        alertmanager_url: str,
        grafana_url: str | None = None,
        rollbacks: RollbackHistory | None = None,
        timeout_seconds: float = 10,
    ) -> None:
        self._kube = kubernetes
        self.namespace = namespace
        self._prometheus_url = prometheus_url
        self._alertmanager_url = alertmanager_url
        self._grafana = grafana_links(grafana_url)
        self._rollbacks = rollbacks
        self._timeout = timeout_seconds

    @classmethod
    def from_settings(cls, settings: Settings) -> Monitoring:
        rollbacks = None
        if settings.database_url:
            rollbacks = PostgresRollbackHistory(
                settings.database_url, cluster=settings.cluster, namespace=settings.namespace
            )
        return cls(
            KubernetesRead(kubeconfig=settings.kubeconfig, context_name=settings.kube_context),
            namespace=settings.namespace,
            prometheus_url=settings.prometheus_url,
            alertmanager_url=settings.alertmanager_url,
            grafana_url=settings.grafana_url,
            rollbacks=rollbacks,
            timeout_seconds=settings.tool_timeout_seconds,
        )

    async def _kube_items(self, resource: MonitoringResource) -> list[dict] | None:
        return await _read(
            asyncio.to_thread(
                self._kube.monitoring_items, self.namespace, resource, timeout=self._timeout
            ),
            self._timeout,
        )

    async def _rollback_items(self, now: datetime) -> list[RollbackRecord] | None:
        if self._rollbacks is None:
            return None
        return await _read(self._rollbacks(now - HISTORY_WINDOW, now), self._timeout)

    async def release_changes(self, *, now: datetime | None = None) -> dict:
        """发布抓取只查询其三个来源。"""
        now = now or datetime.now(UTC)
        deployments, replicasets, rollbacks = await asyncio.gather(
            self._kube_items("deployments"),
            self._kube_items("replicasets"),
            self._rollback_items(now),
        )
        return _project(
            changes,
            self.namespace,
            deployments,
            replicasets,
            rollbacks,
            now,
            source="release_projection",
        )

    async def snapshot(self, *, now: datetime | None = None) -> dict[str, Any]:
        """返回 JSON 形状。服务层负责告警会话关联和共享出站脱敏。"""
        now = (now or datetime.now(UTC)).astimezone(UTC)
        timeout = self._timeout
        async with (
            httpx.AsyncClient(
                base_url=self._prometheus_url, timeout=timeout, trust_env=False
            ) as prom,
            httpx.AsyncClient(
                base_url=self._alertmanager_url, timeout=timeout, trust_env=False
            ) as am,
            asyncio.TaskGroup() as tasks,
        ):
            deployments, replicasets, pods, services, events, metrics = (
                tasks.create_task(self._kube_items(resource))
                for resource in (
                    "deployments",
                    "replicasets",
                    "pods",
                    "services",
                    "events",
                    "metrics",
                )
            )
            restarts = tasks.create_task(
                _read(prometheus_series(prom, restart_query(self.namespace), now), timeout)
            )
            rollbacks = tasks.create_task(self._rollback_items(now))
            alert_panel = tasks.create_task(_panel(alert_groups(am, now), "alertmanager", timeout))
            probe_panel = tasks.create_task(_panel(probes(prom, now), "prometheus", timeout))
            node_panel = tasks.create_task(_panel(nodes(prom, now), "kube_state_metrics", timeout))

        inputs = {
            "deployments": deployments.result(),
            "replicasets": replicasets.result(),
            "pods": pods.result(),
            "services": services.result(),
            "metrics": metrics.result(),
            "restarts": restarts.result(),
        }
        panels = {
            "alerts": alert_panel.result(),
            "probes": probe_panel.result(),
            "changes": _project(
                changes,
                self.namespace,
                inputs["deployments"],
                inputs["replicasets"],
                rollbacks.result(),
                now,
                source="release_projection",
            ),
            "services": _project(service_health, inputs, now, source="service_projection"),
            "warnings": _project(_warnings, events.result(), now, source="event_projection"),
            "nodes": node_panel.result(),
        }
        return {
            "collected_at": iso(now),
            "abnormal_service_count": (panels["services"]["data"] or {}).get("abnormal_count"),
            "grafana_links": self._grafana.copy(),
            **panels,
        }


def _project(function, *args, source: str) -> dict:
    try:
        return function(*args)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return block(failed=[source])
