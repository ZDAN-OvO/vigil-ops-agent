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
    containers = []
    for container in status.get("container_statuses") or []:
        state = container.get("state") or {}
        terminated = state.get("terminated") or {}
        waiting = state.get("waiting") or {}
        containers.append(
            {
                "name": container.get("name"),
                "image": container.get("image"),
                "ready": container.get("ready"),
                "restart_count": container.get("restart_count", 0),
                "phase": "terminated" if terminated else "waiting" if waiting else "running",
                "reason": terminated.get("reason") or waiting.get("reason"),
                "exit_code": terminated.get("exit_code"),
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
    spec = deployment.get("spec") or {}
    status = deployment.get("status") or {}
    pod_spec = (spec.get("template") or {}).get("spec") or {}
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
        "images": [container.get("image") for container in pod_spec.get("containers") or []],
        "selector": (spec.get("selector") or {}).get("match_labels") or {},
    }


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
            response = apps.list_namespaced_deployment(context.namespace, label_selector=selector)
            return self._limit([trim_deployment(item.to_dict()) for item in response.items])

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
