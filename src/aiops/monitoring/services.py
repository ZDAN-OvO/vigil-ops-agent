"""把 Pod 状态和容器用量聚合成 Deployment 或 Service 状态。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from decimal import Decimal, DecimalException
from typing import Any

from kubernetes.utils.quantity import parse_quantity

from aiops.monitoring.releases import controller_uid
from aiops.monitoring.signals import as_time, block, instant_value, iso
from aiops.tools.kubernetes import trim_pod

RESTART_WINDOW = timedelta(hours=1)
RESTART_THRESHOLD = 3
METRICS_MAX_AGE = timedelta(minutes=2)
DURATION = re.compile(r"(\d+(?:\.\d+)?)(h|ms|us|µs|ns|m|s)")
DURATION_SCALE = {"h": 3600, "m": 60, "s": 1, "ms": 1e-3, "us": 1e-6, "µs": 1e-6, "ns": 1e-9}


def restart_query(namespace: str) -> str:
    metric = (
        'kube_pod_container_status_restarts_total{job="kube-state-metrics",'
        f"namespace={json.dumps(namespace)}}}"
    )
    key = "namespace,pod,uid,container"
    return f"max by ({key}) (increase({metric}[1h]) and (timestamp({metric}) > time() - 45))"


def _quantity(value: Any) -> Decimal | None:
    try:
        result = parse_quantity(value)
    except (ValueError, TypeError, DecimalException):
        return None
    if result.is_finite() and result >= 0:
        return result
    return None


def _duration(value: str) -> timedelta | None:
    parts = DURATION.findall(value)
    if not parts or "".join(count + unit for count, unit in parts) != value:
        return None
    return timedelta(seconds=sum(float(count) * DURATION_SCALE[unit] for count, unit in parts))


def _matches(selector: dict, labels: dict) -> bool:
    return bool(selector) and all(labels.get(key) == value for key, value in selector.items())


def _inventory(
    deployments: list[dict], services: list[dict], replicasets: list[dict], pods: list[dict]
):
    by_deployment = {item["metadata"]["uid"]: item for item in deployments}
    by_rs = {item["metadata"]["uid"]: controller_uid(item, "Deployment") for item in replicasets}
    by_service = {
        item["metadata"]["name"]: item["spec"]["selector"]
        for item in services
        if item["spec"].get("selector")
    }
    deployment_labels = [
        (item["spec"]["template"].get("metadata") or {}).get("labels") or {} for item in deployments
    ]
    pods_by_deployment: dict[str, list[dict]] = {uid: [] for uid in by_deployment}
    pods_by_service: dict[str, list[dict]] = {name: [] for name in by_service}
    represented_services = {
        name
        for name, selector in by_service.items()
        if any(_matches(selector, labels) for labels in deployment_labels)
    }
    uncertain_services = set()
    unattributed = False
    for pod in pods:
        meta = pod["metadata"]
        if meta.get("deletion_timestamp") or (pod.get("status") or {}).get("phase") == "Succeeded":
            continue
        rs_uid = controller_uid(pod, "ReplicaSet")
        deployment_uid = by_rs.get(rs_uid) if rs_uid else None
        selected_by = [
            name
            for name, selector in by_service.items()
            if _matches(selector, meta.get("labels") or {})
        ]
        if deployment_uid in by_deployment:
            pods_by_deployment[deployment_uid].append(pod)
            represented_services.update(selected_by)
        elif rs_uid and (rs_uid not in by_rs or deployment_uid is not None):
            # 不能把名字或标签相同、归属 UID 已变化的 Pod 算给新 Deployment。
            unattributed = True
            uncertain_services.update(selected_by)
        elif len(selected_by) == 1:
            pods_by_service[selected_by[0]].append(pod)
        elif len(selected_by) > 1:
            uncertain_services.update(selected_by)
            unattributed = True
    return (
        [(item, pods_by_deployment[uid]) for uid, item in by_deployment.items()],
        [
            (name, attached)
            for name, attached in pods_by_service.items()
            if attached or name not in represented_services or name in uncertain_services
        ],
        uncertain_services,
        unattributed,
    )


def _usage_for_pod(metrics: dict | None, pod: dict, now: datetime) -> dict:
    if metrics is None:
        return {}
    meta = pod["metadata"]
    sample_uid = metrics["metadata"].get("uid")
    if sample_uid and sample_uid != meta["uid"]:
        return {}
    sample_at = as_time(metrics.get("timestamp"))
    born = as_time(meta.get("creation_timestamp"))
    window = _duration(metrics.get("window") or "")
    if not sample_at or not born or not window:
        return {}
    if not now - METRICS_MAX_AGE <= sample_at <= now or sample_at - window < born:
        return {}
    return {container["name"]: container["usage"] for container in metrics["containers"]}


def _container_health(
    raw_pod: dict,
    metrics: dict | None,
    restarts: dict,
    now: datetime,
) -> tuple[list[dict], list[dict], int, int, bool]:
    spec = raw_pod.get("spec") or {}
    status = raw_pod.get("status") or {}
    pod = trim_pod(
        raw_pod
        | {
            "spec": spec
            | {
                "containers": [
                    *(spec.get("containers") or []),
                    *(spec.get("init_containers") or []),
                ]
            },
            "status": status
            | {
                "container_statuses": [
                    *(status.get("container_statuses") or []),
                    *(status.get("init_container_statuses") or []),
                ]
            },
        }
    )
    pod_name = pod["name"]
    issues = [{"kind": "pod_pending", "pod": pod_name}] if pod["phase"] == "Pending" else []
    exits = []
    samples_needed = samples_found = 0
    restart_missing = False
    created = as_time(raw_pod["metadata"].get("creation_timestamp"))
    usage = _usage_for_pod(metrics, raw_pod, now)
    for container in pod["containers"]:
        name = container["name"]
        target = {"pod": pod_name, "container": name}
        last_exit = container["last_terminated"]
        if last_exit:
            exited_at = as_time(last_exit["finished_at"])
            exits.append(
                target
                | {
                    "reason": last_exit["reason"],
                    "exit_code": last_exit["exit_code"],
                    "occurred_at": iso(exited_at),
                }
            )
            if (
                exited_at
                and now - RESTART_WINDOW <= exited_at <= now
                and (last_exit["reason"] == "OOMKilled" or last_exit["exit_code"] not in {0, None})
            ):
                issues.append(
                    target
                    | {
                        "kind": "recent_container_exit",
                        "reason": last_exit["reason"],
                        "occurred_at": iso(exited_at),
                    }
                )
        reason = container["reason"]
        if container["phase"] == "waiting" and reason not in {None, "ContainerCreating"}:
            issues.append(target | {"kind": "container_waiting", "reason": reason})
        if container["phase"] == "terminated" and container["exit_code"] not in {0, None}:
            issues.append(target | {"kind": "container_terminated", "reason": reason})
        count = container["restart_count"] or 0
        if count >= RESTART_THRESHOLD:
            recent = (
                count
                if created and now - RESTART_WINDOW <= created <= now
                else restarts.get((pod_name, pod["uid"], name))
            )
            if recent is None:
                restart_missing = True
            elif recent >= RESTART_THRESHOLD:
                issues.append(
                    target
                    | {
                        "kind": "frequent_restarts",
                        "restart_count": count,
                        "restart_count_last_hour": recent,
                        "estimated": not (created and now - RESTART_WINDOW <= created <= now),
                        "occurred_at": exits[-1]["occurred_at"] if last_exit else None,
                    }
                )

        if container["phase"] != "running":
            continue
        limits = container["resources"]["limits"]
        for resource, unit in (("cpu", "cores"), ("memory", "bytes")):
            if resource not in limits:
                continue
            samples_needed += 1
            limit = _quantity(limits[resource])
            used = _quantity(usage.get(name, {}).get(resource))
            if limit is None or limit <= 0 or used is None:
                continue
            samples_found += 1
            if used / limit > Decimal("0.8"):
                issues.append(
                    target
                    | {
                        "kind": "resource_near_limit",
                        "resource": resource,
                        "usage": float(used),
                        "limit": float(limit),
                        "unit": unit,
                        "ratio": float(used / limit),
                    }
                )
    return issues, exits, samples_needed, samples_found, restart_missing


def _row(
    name: str,
    kind: str,
    pods: list[dict],
    base_issues: list[dict],
    readiness_missing: bool,
    usage: dict,
    restarts: dict,
    now: datetime,
) -> dict:
    issues = base_issues
    last_exits = []
    needed = found = 0
    restarts_missing = False
    for pod in pods:
        pod_issues, exits, samples_needed, samples_found, missing = _container_health(
            pod, usage.get(pod["metadata"]["name"]), restarts, now
        )
        issues.extend(pod_issues)
        last_exits.extend(exits)
        needed += samples_needed
        found += samples_found
        restarts_missing |= missing
    if found == needed:
        resource_status = "ok" if needed else "not_applicable"
    else:
        resource_status = "partial" if found else "missing"
    missing = []
    if readiness_missing:
        missing.append("readiness")
    if found < needed:
        missing.append("container_usage")
    if restarts_missing:
        missing.append("recent_restarts")
    return {
        "name": name,
        "kind": kind,
        "issues": issues,
        "last_exits": last_exits,
        "resource_status": resource_status,
        "missing": missing,
    }


def service_health(
    sources: dict[str, list | None],
    now: datetime,
) -> dict:
    primary = ("deployments", "replicasets", "pods", "services")
    failed = [key for key in primary if sources[key] is None]
    if failed:
        return block(failed=failed)
    deployments, services, ambiguous, unattributed = _inventory(
        sources["deployments"], sources["services"], sources["replicasets"], sources["pods"]
    )
    metrics = {item["metadata"]["name"]: item for item in sources["metrics"] or []}
    restarts = {
        (row["labels"]["pod"], row["labels"]["uid"], row["labels"]["container"]): instant_value(row)
        for row in sources["restarts"] or []
    }
    rows = []
    for deployment, pods in deployments:
        status = deployment["status"] or {}
        spec = deployment["spec"]
        meta = deployment["metadata"]
        desired = spec.get("replicas")
        desired = desired if desired is not None else 1
        generation = meta.get("generation")
        observed = status.get("observed_generation")
        readiness_missing = generation is None or observed is None or observed < generation
        ready = status.get("ready_replicas") or 0
        issues = (
            [{"kind": "replicas_not_ready", "desired": desired, "ready": ready}]
            if not readiness_missing and ready < desired
            else []
        )
        rows.append(
            _row(
                meta["name"], "Deployment", pods, issues, readiness_missing, metrics, restarts, now
            )
        )
    for name, pods in services:
        statuses = [trim_pod(pod)["conditions"].get("Ready") for pod in pods]
        readiness_missing = name in ambiguous or any(state is None for state in statuses)
        ready = statuses.count("True")
        issues = []
        if name not in ambiguous:
            if not pods:
                issues.append({"kind": "no_ready_pods"})
            elif any(state in {"False", "Unknown"} for state in statuses):
                issues.append(
                    {
                        "kind": "replicas_not_ready",
                        "desired": len(pods),
                        "ready": None if readiness_missing else ready,
                    }
                )
        rows.append(_row(name, "Service", pods, issues, readiness_missing, metrics, restarts, now))

    rows.sort(key=lambda row: (row["name"], row["kind"]))
    abnormal = [row for row in rows if row["issues"]]
    unknown = [row for row in rows if row["missing"] and not row["issues"]]
    healthy = [
        {"name": row["name"], "kind": row["kind"]}
        for row in rows
        if not row["missing"] and not row["issues"]
    ]
    missing = {reason for row in rows for reason in row["missing"]}
    if unattributed:
        missing.add("pod_ownership")
    if sources["metrics"] is None and "container_usage" in missing:
        failed.append("metrics")
    if sources["restarts"] is None and "recent_restarts" in missing:
        failed.append("restarts")
    return block(
        {
            "abnormal_count": None if unknown or unattributed else len(abnormal),
            "abnormal": abnormal,
            "healthy": healthy,
            "unknown": unknown,
        },
        missing=sorted(missing),
        failed=failed,
    )
