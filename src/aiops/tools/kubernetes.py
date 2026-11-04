"""Kubernetes 只读适配器：白名单对象、固定 namespace、结果裁剪后交给模型。"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.client.exceptions import ApiException

from aiops.contracts import TimeRange, ToolError, ToolResult
from aiops.tools.base import ToolContext


def trim_pod(pod: dict[str, Any]) -> dict[str, Any]:
    metadata = pod.get("metadata") or {}
    spec = pod.get("spec") or {}
    status = pod.get("status") or {}
    spec_containers = {
        container.get("name"): container for container in spec.get("containers") or []
    }
    containers = []
    for container in status.get("container_statuses") or []:
        state = container.get("state") or {}
        terminated = state.get("terminated") or {}
        waiting = state.get("waiting") or {}
        running = state.get("running") or {}
        last_terminated = (container.get("last_state") or {}).get("terminated") or {}
        resources = (spec_containers.get(container.get("name")) or {}).get("resources") or {}
        containers.append(
            {
                "name": container.get("name"),
                "image": container.get("image"),
                "ready": container.get("ready"),
                "restart_count": container.get("restart_count", 0),
                "phase": "terminated" if terminated else "waiting" if waiting else "running",
                "reason": terminated.get("reason") or waiting.get("reason"),
                "exit_code": terminated.get("exit_code"),
                "started_at": running.get("started_at"),
                "last_terminated": (
                    {
                        "reason": last_terminated.get("reason"),
                        "exit_code": last_terminated.get("exit_code"),
                        "finished_at": last_terminated.get("finished_at"),
                    }
                    if last_terminated
                    else None
                ),
                "resources": {
                    "limits": resources.get("limits") or {},
                    "requests": resources.get("requests") or {},
                },
            }
        )
    return {
        "name": metadata.get("name"),
        "uid": metadata.get("uid"),
        "labels": metadata.get("labels") or {},
        "node": spec.get("node_name"),
        "phase": status.get("phase"),
        "containers": containers,
        "conditions": {
            condition.get("type"): condition.get("status")
            for condition in status.get("conditions") or []
        },
    }


def trim_deployment(deployment: dict[str, Any]) -> dict[str, Any]:
    metadata = deployment.get("metadata") or {}
    annotations = metadata.get("annotations") or {}
    spec = deployment.get("spec") or {}
    status = deployment.get("status") or {}
    pod_spec = (spec.get("template") or {}).get("spec") or {}
    containers = []
    for container in pod_spec.get("containers") or []:
        resources = container.get("resources") or {}
        containers.append(
            {
                "name": container.get("name"),
                "image": container.get("image"),
                "limits": resources.get("limits") or {},
                "requests": resources.get("requests") or {},
            }
        )
    return {
        "name": metadata.get("name"),
        "uid": metadata.get("uid"),
        "generation": metadata.get("generation"),
        "observed_generation": status.get("observed_generation"),
        "replicas": status.get("replicas"),
        "updated_replicas": status.get("updated_replicas"),
        "available_replicas": status.get("available_replicas"),
        "unavailable_replicas": status.get("unavailable_replicas"),
        "strategy": (spec.get("strategy") or {}).get("type"),
        "labels": metadata.get("labels") or {},
        "revision": annotations.get("deployment.kubernetes.io/revision"),
        "change_cause": annotations.get("kubernetes.io/change-cause"),
        "images": [container.get("image") for container in pod_spec.get("containers") or []],
        "containers": containers,
        "selector": (spec.get("selector") or {}).get("match_labels") or {},
    }


def trim_replica_set(replicaset: dict[str, Any]) -> dict[str, Any]:
    metadata = replicaset.get("metadata") or {}
    spec = replicaset.get("spec") or {}
    status = replicaset.get("status") or {}
    annotations = metadata.get("annotations") or {}
    template_spec = (spec.get("template") or {}).get("spec") or {}
    containers = template_spec.get("containers") or []
    container = containers[0] if containers else {}
    resources = container.get("resources") or {}
    return {
        "revision": int(annotations.get("deployment.kubernetes.io/revision", "0") or 0),
        "image": container.get("image"),
        "memory_limit": (resources.get("limits") or {}).get("memory"),
        "memory_request": (resources.get("requests") or {}).get("memory"),
        "replicas": spec.get("replicas"),
        "ready_replicas": status.get("ready_replicas") or 0,
        "created_at": metadata.get("creation_timestamp"),
        "owners": [
            {"kind": owner.get("kind"), "uid": owner.get("uid")}
            for owner in metadata.get("owner_references") or []
        ],
    }


def matches_selector(selector: str, item: dict[str, Any]) -> bool:
    """按 Deployment 自身标签或 Pod 选择器匹配；没有标签的 Deployment 也能被 app=x 命中。"""
    pairs = [part for part in selector.split(",") if "=" in part]
    if not pairs:
        return True
    labels = {**(item.get("labels") or {}), **(item.get("selector") or {})}
    for part in pairs:
        key, value = (piece.strip() for piece in part.split("=", 1))
        if labels.get(key) != value:
            return False
    return True


def trim_event(event: dict[str, Any]) -> dict[str, Any]:
    involved = event.get("involved_object") or {}
    return {
        "name": (event.get("metadata") or {}).get("name"),
        "type": event.get("type"),
        "reason": event.get("reason"),
        "message": event.get("message"),
        "count": event.get("count"),
        "involved": {"kind": involved.get("kind"), "name": involved.get("name")},
        "first_seen": event.get("first_timestamp"),
        "last_seen": event.get("last_timestamp"),
    }


class KubernetesRead:
    """只暴露 get/list；不提供 create、patch、delete 或任意命令执行。"""

    def __init__(
        self,
        *,
        kubeconfig: Path | None = None,
        context_name: str | None = None,
        max_items: int = 100,
    ) -> None:
        self._kubeconfig = kubeconfig
        self._context_name = context_name
        self._max_items = max_items
        self._loaded = False
        self._lock = Lock()
        self._core: k8s_client.CoreV1Api | None = None
        self._apps: k8s_client.AppsV1Api | None = None

    def _clients(self) -> tuple[k8s_client.CoreV1Api, k8s_client.AppsV1Api]:
        with self._lock:
            if not self._loaded:
                if self._kubeconfig is not None:
                    k8s_config.load_kube_config(
                        config_file=str(self._kubeconfig), context=self._context_name
                    )
                else:
                    try:
                        k8s_config.load_incluster_config()
                    except k8s_config.ConfigException:
                        k8s_config.load_kube_config(context=self._context_name)
                api = k8s_client.ApiClient()
                self._core = k8s_client.CoreV1Api(api)
                self._apps = k8s_client.AppsV1Api(api)
                self._loaded = True
        if self._core is None or self._apps is None:
            raise RuntimeError("Kubernetes 客户端初始化失败")
        return self._core, self._apps

    def _limit(self, items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
        return items[: self._max_items], len(items) > self._max_items

    async def _call(
        self,
        context: ToolContext,
        time_range: TimeRange,
        query: dict[str, Any],
        call: Callable[[], tuple[list[dict[str, Any]], bool]],
    ) -> ToolResult:
        tool_call_id = uuid.uuid4().hex
        reason = context.abort_reason()
        if reason is not None:
            return self._failure(
                tool_call_id, time_range, query, reason, "调用前已取消或超过截止时间"
            )
        try:
            items, truncated = await asyncio.wait_for(
                asyncio.to_thread(call), timeout=context.remaining()
            )
        except TimeoutError:
            return self._failure(tool_call_id, time_range, query, "timeout", "查询超过截止时间")
        except ApiException as exc:
            code = "auth" if exc.status in {401, 403} else "kubernetes_api"
            return self._failure(
                tool_call_id, time_range, query, code, f"Kubernetes API 返回 {exc.status}"
            )

        raw = context.persist_raw(
            tool_call_id, {"query": query, "items": items, "truncated": truncated}
        )
        return ToolResult(
            tool_call_id=tool_call_id,
            source="kubernetes",
            query=query,
            time_range=time_range,
            collected_at=datetime.now(UTC),
            data={"items": items, "count": len(items)},
            raw_ref=raw.ref,
            truncated=truncated,
        )

    def _failure(
        self,
        tool_call_id: str,
        time_range: TimeRange,
        query: dict[str, Any],
        code: str,
        message: str,
    ) -> ToolResult:
        return ToolResult(
            tool_call_id=tool_call_id,
            source="kubernetes",
            query=query,
            time_range=time_range,
            collected_at=datetime.now(UTC),
            error=ToolError(code=code, message=message, retryable=code == "timeout"),
        )

    async def list_pods(
        self,
        context: ToolContext,
        time_range: TimeRange,
        *,
        selector: str | None = None,
    ) -> ToolResult:
        core, _ = self._clients()
        query = {"kind": "pods", "namespace": context.namespace, "selector": selector}

        def call() -> tuple[list[dict[str, Any]], bool]:
            response = core.list_namespaced_pod(context.namespace, label_selector=selector)
            return self._limit([trim_pod(item.to_dict()) for item in response.items])

        return await self._call(context, time_range, query, call)

    async def list_deployments(
        self,
        context: ToolContext,
        time_range: TimeRange,
        *,
        selector: str | None = None,
    ) -> ToolResult:
        _, apps = self._clients()
        query = {"kind": "deployments", "namespace": context.namespace, "selector": selector}

        def call() -> tuple[list[dict[str, Any]], bool]:
            response = apps.list_namespaced_deployment(context.namespace)
            # 历史模板是回退适用性的证据，按 owner 归到各自 Deployment。
            history: dict[str, list[dict[str, Any]]] = {}
            for item in apps.list_namespaced_replica_set(context.namespace).items:
                replicaset = trim_replica_set(item.to_dict())
                for owner in replicaset.pop("owners"):
                    if owner.get("kind") == "Deployment" and owner.get("uid"):
                        history.setdefault(owner["uid"], []).append(dict(replicaset))
            items = []
            for deployment in response.items:
                trimmed = trim_deployment(deployment.to_dict())
                if selector and not matches_selector(selector, trimmed):
                    continue
                revisions = sorted(
                    history.get(deployment.metadata.uid, []), key=lambda row: row["revision"]
                )
                trimmed["revisions"] = revisions[-5:]
                items.append(trimmed)
            return self._limit(items)

        return await self._call(context, time_range, query, call)

    async def list_events(
        self,
        context: ToolContext,
        time_range: TimeRange,
        *,
        involved_name: str | None = None,
    ) -> ToolResult:
        core, _ = self._clients()
        field_selector = f"involvedObject.name={involved_name}" if involved_name else None
        query = {
            "kind": "events",
            "namespace": context.namespace,
            "involved_object": involved_name,
        }

        def call() -> tuple[list[dict[str, Any]], bool]:
            response = core.list_namespaced_event(context.namespace, field_selector=field_selector)
            return self._limit([trim_event(item.to_dict()) for item in response.items])

        return await self._call(context, time_range, query, call)
