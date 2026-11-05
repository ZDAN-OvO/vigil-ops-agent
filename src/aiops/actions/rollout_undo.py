"""rollout_undo 适配器：回退到上一版本模板，并观察真实业务恢复。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.client.exceptions import ApiException

REVISION_ANNOTATION = "deployment.kubernetes.io/revision"
CHANGE_CAUSE = "kubernetes.io/change-cause"


class ActionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RolloutPlan:
    namespace: str
    name: str
    current_revision: int
    target_revision: int
    current_image: str
    target_image: str
    template: dict[str, Any]
    deployment_uid: str
    resource_version: str
    current_memory_limit: str | None = None
    target_memory_limit: str | None = None
    current_memory_request: str | None = None
    target_memory_request: str | None = None


def _container_memory(container: Any) -> tuple[str | None, str | None]:
    resources = container.resources or {}
    limits = resources.limits or {}
    requests = resources.requests or {}
    return limits.get("memory"), requests.get("memory")


def _clean_template(template: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(template.get("metadata") or {})
    for key in (
        "creationTimestamp",
        "resourceVersion",
        "uid",
        "managedFields",
        "generation",
        "selfLink",
        "name",
        "namespace",
        "ownerReferences",
        "finalizers",
        "deletionGracePeriodSeconds",
        "deletionTimestamp",
        "generateName",
    ):
        metadata.pop(key, None)
    labels = dict(metadata.get("labels") or {})
    labels.pop("pod-template-hash", None)
    metadata["labels"] = labels
    if metadata.get("annotations") is None:
        metadata.pop("annotations", None)
    return {"metadata": metadata, "spec": template.get("spec") or {}}


class RolloutUndoAdapter:
    def __init__(self, *, kubeconfig: Path | None = None, context_name: str | None = None) -> None:
        self._kubeconfig = kubeconfig
        self._context_name = context_name
        self._loaded = False
        self._lock = Lock()
        self._client: k8s_client.ApiClient | None = None
        self._apps: k8s_client.AppsV1Api | None = None
        self._core: k8s_client.CoreV1Api | None = None

    def _api(self) -> k8s_client.AppsV1Api:
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
                self._client = k8s_client.ApiClient()
                self._apps = k8s_client.AppsV1Api(self._client)
                self._core = k8s_client.CoreV1Api(self._client)
                self._loaded = True
        if self._apps is None or self._client is None:
            raise ActionError("client_error", "Kubernetes 客户端初始化失败")
        return self._apps

    def _core_api(self) -> k8s_client.CoreV1Api:
        self._api()
        if self._core is None:
            raise ActionError("client_error", "Kubernetes 客户端初始化失败")
        return self._core

    def plan(self, namespace: str, name: str) -> RolloutPlan:
        """计算回退目标；任何不满足前置条件的组合都拒绝。"""
        apps = self._api()
        try:
            deployment = apps.read_namespaced_deployment(name, namespace)
        except ApiException as exc:
            code = "not_found" if exc.status == 404 else "api_error"
            raise ActionError(code, f"读取 Deployment 失败：HTTP {exc.status}") from exc

        annotations = deployment.metadata.annotations or {}
        current_revision = int(annotations.get(REVISION_ANNOTATION, "1"))
        selector = ",".join(
            f"{key}={value}"
            for key, value in sorted((deployment.spec.selector.match_labels or {}).items())
        )
        try:
            replicasets = apps.list_namespaced_replica_set(namespace, label_selector=selector).items
        except ApiException as exc:
            raise ActionError("api_error", f"读取 ReplicaSet 失败：HTTP {exc.status}") from exc

        by_revision: dict[int, Any] = {}
        for replicaset in replicasets:
            revision = (replicaset.metadata.annotations or {}).get(REVISION_ANNOTATION)
            if revision is None:
                continue
            by_revision[int(revision)] = replicaset

        older = [revision for revision in by_revision if revision < current_revision]
        if not older:
            raise ActionError("no_previous_revision", "没有可回退的历史 revision")
        target_revision = max(older)
        replicaset = by_revision[target_revision]

        current_container = deployment.spec.template.spec.containers[0]
        target_container = replicaset.spec.template.spec.containers[0]
        current_image = current_container.image
        target_image = target_container.image
        current_template = _clean_template(
            self._client.sanitize_for_serialization(deployment.spec.template)
        )
        target_template = _clean_template(
            self._client.sanitize_for_serialization(replicaset.spec.template)
        )
        if current_template == target_template:
            raise ActionError("same_template", "上一 revision 的 Pod 模板与当前相同，回退没有意义")

        current_memory_limit, current_memory_request = _container_memory(current_container)
        target_memory_limit, target_memory_request = _container_memory(target_container)
        return RolloutPlan(
            namespace=namespace,
            name=name,
            current_revision=current_revision,
            target_revision=target_revision,
            current_image=current_image,
            target_image=target_image,
            template=target_template,
            deployment_uid=deployment.metadata.uid,
            resource_version=deployment.metadata.resource_version,
            current_memory_limit=current_memory_limit,
            target_memory_limit=target_memory_limit,
            current_memory_request=current_memory_request,
            target_memory_request=target_memory_request,
        )

    def apply(self, plan: RolloutPlan, *, dry_run: bool, proposal_id: str) -> dict[str, Any]:
        """把上一 revision 的 Pod 模板写回 Deployment；resourceVersion 作为并发前置。"""
        apps = self._api()
        body = {
            "metadata": {
                "resourceVersion": plan.resource_version,
                "annotations": {CHANGE_CAUSE: f"vigil rollback {proposal_id}"},
            },
            "spec": {"template": plan.template},
        }
        try:
            apps.patch_namespaced_deployment(
                plan.name,
                plan.namespace,
                body,
                dry_run="All" if dry_run else None,
            )
        except ApiException as exc:
            raise ActionError("api_error", f"写入 Deployment 失败：HTTP {exc.status}") from exc
        return {"revision": plan.target_revision, "dry_run": dry_run}

    def wait_rolled_out(self, plan: RolloutPlan, timeout_seconds: float) -> bool:
        apps = self._api()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            deployment = apps.read_namespaced_deployment(plan.name, plan.namespace)
            status = deployment.status
            replicas = deployment.spec.replicas or 1
            if (
                deployment.metadata.generation == status.observed_generation
                and (status.updated_replicas or 0) == replicas
                and (status.available_replicas or 0) == replicas
                and (status.unavailable_replicas or 0) == 0
            ):
                return True
            time.sleep(2)
        return False

    def state(self, namespace: str, name: str) -> dict[str, Any]:
        apps = self._api()
        core = self._core_api()
        deployment = apps.read_namespaced_deployment(name, namespace)
        annotations = deployment.metadata.annotations or {}
        container = deployment.spec.template.spec.containers[0]
        memory_limit, memory_request = _container_memory(container)
        selector = ",".join(
            f"{key}={value}"
            for key, value in sorted((deployment.spec.selector.match_labels or {}).items())
        )
        try:
            pods = core.list_namespaced_pod(namespace, label_selector=selector).items
        except ApiException as exc:
            raise ActionError("api_error", f"读取 Pod 失败：HTTP {exc.status}") from exc
        pod_states = []
        for pod in pods:
            for status in pod.status.container_statuses or []:
                last = status.last_state.terminated if status.last_state else None
                pod_states.append(
                    {
                        "name": pod.metadata.name,
                        "uid": pod.metadata.uid,
                        "phase": pod.status.phase,
                        "ready": bool(status.ready),
                        "restarts": status.restart_count or 0,
                        "last_terminated_reason": last.reason if last else None,
                        "last_terminated_exit_code": last.exit_code if last else None,
                    }
                )
        return {
            "uid": deployment.metadata.uid,
            "resource_version": deployment.metadata.resource_version,
            "revision": int(annotations.get(REVISION_ANNOTATION, "1")),
            "image": container.image,
            "memory_limit": memory_limit,
            "memory_request": memory_request,
            "replicas": deployment.spec.replicas,
            "available_replicas": deployment.status.available_replicas,
            "pods": pod_states,
        }
