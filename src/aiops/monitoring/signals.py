"""固定 Prometheus 与 AlertManager 查询，以及入口探测和节点投影。"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from math import isfinite
from typing import Any

import httpx

from aiops.tools.prometheus import parse_series

PROBE_STEP_SECONDS = 15
PROBE_POINTS = 3600 // PROBE_STEP_SECONDS
PROBE_JOB = 'job="vigil-entry-probe"'
PROBE_SIGNALS = {
    "up": "up",
    "success": "probe_success",
    "http_status": "probe_http_status_code",
    "duration": "probe_duration_seconds",
}
PROBE_QUERY = " or ".join(
    f'label_replace(last_over_time({metric}{{{PROBE_JOB}}}[15s]),"signal","{signal}","job",".+")'
    for signal, metric in PROBE_SIGNALS.items()
)
NODE_QUERY = (
    'kube_node_info{job="kube-state-metrics"} or '
    'kube_node_status_condition{job="kube-state-metrics",'
    'condition=~"Ready|MemoryPressure|DiskPressure|PIDPressure"}'
)
NODE_CONDITIONS = ("Ready", "MemoryPressure", "DiskPressure", "PIDPressure")
ALERT_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
WEBHOOK_ALERTS = frozenset(
    {
        "FrontendProbeFailed",
        "VigilCpuLimitDegraded",
        "VigilReplicaCapacityDegraded",
        "VigilServiceConfigDegraded",
    }
)


def number(value: object) -> float | None:
    if isinstance(value, int | float) and isfinite(value):
        return float(value)
    return None


def instant_value(series: dict) -> float | None:
    points = series["points"]
    return number(points[-1][1]) if points else None


def as_time(value: datetime | str | None) -> datetime | None:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC)
    return None


def iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def block(
    data: Any = None, *, missing: list[str] | None = None, failed: list[str] | None = None
) -> dict:
    if data is None:
        status = "error" if failed else "missing"
    else:
        status = "partial" if missing or failed else "ok"
    return {"status": status, "data": data, "missing": missing or [], "failed": failed or []}


async def prometheus_series(
    client: httpx.AsyncClient,
    query: str,
    now: datetime,
    *,
    range_start: float | None = None,
) -> list[dict]:
    params = {"query": query}
    if range_start is None:
        path = "/api/v1/query"
        params["time"] = str(now.timestamp())
    else:
        path = "/api/v1/query_range"
        params |= {
            "start": str(range_start),
            "end": str(now.timestamp()),
            "step": f"{PROBE_STEP_SECONDS}s",
        }
    response = await client.get(path, params=params)
    response.raise_for_status()
    payload = response.json()
    if payload["status"] != "success" or payload.get("warnings"):
        raise ValueError("Prometheus 查询未返回完整结果")
    expected = "vector" if range_start is None else "matrix"
    if payload["data"]["resultType"] != expected:
        raise ValueError("Prometheus 返回了意外的结果类型")
    series, truncated = parse_series(payload, max_points=PROBE_POINTS, max_series=10000)
    if truncated:
        raise ValueError("Prometheus 查询结果超出面板上限")
    return series


def alert_group_key(receiver: str, name: str, labels: dict[str, str] | None = None) -> str | None:
    """对应实验清单的分组标签，兼容历史仅按告警名分组的会话。"""
    if not ALERT_NAME.fullmatch(name):
        return None
    group_labels = "{alertname=" + json.dumps(name) + "}"
    labels = labels or {"alertname": name}
    if set(labels) - {"alertname", "namespace", "source", "target"}:
        return None
    encoded = (
        "{"
        + ", ".join(
            key + "=" + json.dumps(value, ensure_ascii=False)
            for key, value in sorted(labels.items())
        )
        + "}"
    )
    if receiver == "default" and name not in WEBHOOK_ALERTS:
        return "{}:" + encoded
    if receiver == "vigil-webhook" and name in WEBHOOK_ALERTS:
        return "{}/" + group_labels + ":" + encoded
    return None


async def alert_groups(client: httpx.AsyncClient, now: datetime) -> dict:
    response = await client.get("/api/v2/alerts/groups")
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        raise ValueError("AlertManager 分组响应格式错误")
    groups = []
    incomplete = False
    for row in rows:
        labels = row["labels"]
        name = labels.get("alertname")
        receiver = row["receiver"]["name"]
        key = alert_group_key(receiver, name, labels) if name else None
        alerts = []
        for alert in row["alerts"]:
            started = as_time(alert["startsAt"])
            ended = as_time(alert["endsAt"])
            state = alert["status"]["state"]
            if state not in {"active", "suppressed"} or (ended and ended <= now):
                continue
            alerts.append(
                {
                    "fingerprint": alert["fingerprint"],
                    "severity": alert["labels"].get("severity"),
                    "starts_at": iso(started),
                    "duration_seconds": max(0, int((now - started).total_seconds()))
                    if started
                    else None,
                    "suppressed": state == "suppressed",
                }
            )
        if alerts:
            incomplete |= key is None
            groups.append(
                {
                    "name": name,
                    "group_key": key,
                    "receiver": receiver,
                    "alerts": sorted(alerts, key=lambda alert: alert["fingerprint"]),
                    "session": None,
                }
            )
    groups.sort(key=lambda group: (group["name"] or "", group["receiver"]))
    return block({"groups": groups}, missing=["alert_group_key"] if incomplete else [])


async def probes(client: httpx.AsyncClient, now: datetime) -> dict:
    end = int(now.timestamp()) // PROBE_STEP_SECONDS * PROBE_STEP_SECONDS
    start = end - (PROBE_POINTS - 1) * PROBE_STEP_SECONDS
    series = await prometheus_series(
        client, PROBE_QUERY, datetime.fromtimestamp(end, UTC), range_start=start
    )
    targets: dict[tuple[str, str], dict[str, dict]] = {}
    for row in series:
        labels = row["labels"]
        key = (labels["application"], labels["address"])
        signals = targets.setdefault(key, {})
        signal = labels["signal"]
        if signal in signals:
            raise ValueError("探测应用和地址标签重复")
        signals[signal] = dict(row["points"])

    applications: dict[str, list[dict]] = {}
    missing = False
    for (application, address), signals in sorted(targets.items()):
        up = signals.get("up", {})
        success = signals.get("success", {})
        points = []
        for slot in range(start, end + 1, PROBE_STEP_SECONDS):
            value = number(success.get(slot))
            points.append([slot, value if up.get(slot) == 1 and value in {0, 1} else None])
        known = [value for _, value in points if value is not None]
        current = points[-1][1]
        status = number(signals.get("http_status", {}).get(end)) if current is not None else None
        duration = number(signals.get("duration", {}).get(end)) if current is not None else None
        missing |= len(known) != PROBE_POINTS
        applications.setdefault(application, []).append(
            {
                "address": address,
                "state": {1: "up", 0: "down", None: "missing"}[current],
                "http_status": int(status) if status and 100 <= status < 600 else None,
                "duration_seconds": duration,
                "availability": sum(known) / len(known) if known else None,
                "sample_count": len(known),
                "points": points,
            }
        )
    if not applications:
        return block(missing=["probe_targets"])
    return block(
        {
            "step_seconds": PROBE_STEP_SECONDS,
            "applications": [
                {"application": name, "targets": rows} for name, rows in applications.items()
            ],
        },
        missing=["probe_samples"] if missing else [],
    )


async def nodes(client: httpx.AsyncClient, now: datetime) -> dict:
    rows = await prometheus_series(client, NODE_QUERY, now)
    inventory: set[str] = set()
    status_by_node: dict[str, dict[str, set[str]]] = {}
    for row in rows:
        if instant_value(row) != 1:
            continue
        labels = row["labels"]
        node = labels["node"]
        if "condition" not in labels:
            inventory.add(node)
        else:
            conditions = status_by_node.setdefault(node, {})
            conditions.setdefault(labels["condition"], set()).add(labels["status"])

    if not inventory and not status_by_node:
        return block(missing=["node_conditions"])

    healthy, abnormal, unknown = [], [], []
    for node in sorted(inventory | status_by_node.keys()):
        conditions = status_by_node.get(node, {})
        known = {key: next(iter(values)) for key, values in conditions.items() if len(values) == 1}
        reasons = [
            {"condition": key, "status": known[key]}
            for key in NODE_CONDITIONS
            if (key == "Ready" and known.get(key) in {"false", "unknown"})
            or (key != "Ready" and known.get(key) == "true")
        ]
        absent = [
            key
            for key in NODE_CONDITIONS
            if key not in known or (key != "Ready" and known[key] == "unknown")
        ]
        if reasons:
            abnormal.append({"name": node, "conditions": reasons})
        elif not absent:
            healthy.append(node)
        if absent:
            unknown.append({"name": node, "missing_conditions": absent})
    missing = []
    if unknown:
        missing.append("node_conditions")
    if not inventory or status_by_node.keys() - inventory:
        missing.append("node_inventory")
    return block({"healthy": healthy, "abnormal": abnormal, "unknown": unknown}, missing=missing)
