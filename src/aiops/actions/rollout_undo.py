"""rollout_undo 适配器：回退到上一版本模板，并观察真实业务恢复。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

from kubernetes import client as k8s_client
from kubernetes.client.exceptions import ApiException

from aiops.actions.kubernetes_write import WRITE_TIMEOUT_SECONDS, action_client

REVISION_ANNOTATION = "deployment.kubernetes.io/revision"
CHANGE_CAUSE = "kubernetes.io/change-cause"
FENCE_ANNOTATION = "vigil.io/reconciliation"


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
    replica_set_name: str = ""
    replica_set_uid: str = ""
    replica_set_version: str = ""
    changes: list[dict[str, Any]] = field(default_factory=list)

    def approved_parameters(self) -> dict[str, Any]:
        return {
            "from_revision": self.current_revision,
            "to_revision": self.target_revision,
            "target_image": self.target_image,
            "from_memory_limit": self.current_memory_limit,
            "to_memory_limit": self.target_memory_limit,
            "workload": self.deployment_uid,
            "replica_set": {
                "name": self.replica_set_name,
                "uid": self.replica_set_uid,
                "resource_version": self.replica_set_version,
            },
            "template": self.template,
            "template_diff": self.changes,
        }


def _container_memory(container: Any) -> tuple[str | None, str | None]:
    resources = container.resources
    if resources is None:
        return None, None
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


def _pointer(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def _template_diff(old: Any, new: Any, path: str = "") -> list[dict[str, Any]]:
    if old == new:
        return []
    changes = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(old.keys() | new.keys()):
            child = f"{path}/{_pointer(key)}"
            if key not in old:
                changes.append({"op": "add", "path": child, "value": new[key]})
            elif key not in new:
                changes.append({"op": "remove", "path": child})
            else:
                changes.extend(_template_diff(old[key], new[key], child))
        return changes
    if isinstance(old, list) and isinstance(new, list):
        for index, (before, after) in enumerate(zip(old, new, strict=False)):
            changes.extend(_template_diff(before, after, f"{path}/{index}"))
        for index in reversed(range(len(new), len(old))):
            changes.append({"op": "remove", "path": f"{path}/{index}"})
        for index in range(len(old), len(new)):
            changes.append({"op": "add", "path": f"{path}/{index}", "value": new[index]})
        return changes
    return [{"op": "replace", "path": path, "value": new}]


def _identity_tests(uid: str, version: str) -> list[dict[str, str]]:
    return [
        {"op": "test", "path": "/metadata/uid", "value": uid},
        {"op": "test", "path": "/metadata/resourceVersion", "value": version},
    ]


def _owned_by(item: Any, kind: str, uid: str, *, name: str | None = None) -> bool:
    return any(
        ref.controller
        and ref.kind == kind
        and ref.api_version == "apps/v1"
        and ref.uid == uid
        and (name is None or ref.name == name)
        for ref in item.metadata.owner_references or []
    )


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
                self._client = action_client(self._kubeconfig, self._context_name)
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
            deployment = apps.read_namespaced_deployment(
                name, namespace, _request_timeout=WRITE_TIMEOUT_SECONDS
            )
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
            replicasets = apps.list_namespaced_replica_set(
                namespace,
                label_selector=selector,
                _request_timeout=WRITE_TIMEOUT_SECONDS,
            ).items
        except ApiException as exc:
            raise ActionError("api_error", f"读取 ReplicaSet 失败：HTTP {exc.status}") from exc

        by_revision: dict[int, Any] = {}
        for replicaset in replicasets:
            if not _owned_by(replicaset, "Deployment", deployment.metadata.uid, name=name):
                continue
            revision = (replicaset.metadata.annotations or {}).get(REVISION_ANNOTATION)
            if revision is None:
                continue
            number = int(revision)
            if number in by_revision:
                raise ActionError("ambiguous_revision", "同一 Deployment 存在重复历史 revision")
            by_revision[number] = replicaset

        older = [revision for revision in by_revision if revision < current_revision]
        if not older:
            raise ActionError("no_previous_revision", "没有可回退的历史 revision")
        target_revision = max(older)
        replicaset = by_revision[target_revision]
        if not (
            deployment.metadata.uid
            and deployment.metadata.resource_version
            and replicaset.metadata.uid
            and replicaset.metadata.resource_version
        ):
            raise ActionError("identity_unknown", "无法确认 Deployment 或历史 ReplicaSet 身份")

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
            replica_set_name=replicaset.metadata.name,
            replica_set_uid=replicaset.metadata.uid,
            replica_set_version=replicaset.metadata.resource_version,
            changes=_template_diff(current_template, target_template),
        )

    def apply(self, plan: RolloutPlan, *, dry_run: bool, proposal_id: str) -> dict[str, Any]:
        """dry-run 与发送共用完整模板替换和身份前置条件。"""
        body = [
            *_identity_tests(plan.deployment_uid, plan.resource_version),
            {"op": "replace", "path": "/spec/template", "value": plan.template},
            {
                "op": "add",
                "path": f"/metadata/annotations/{_pointer(CHANGE_CAUSE)}",
                "value": f"vigil rollback {proposal_id}",
            },
        ]
        try:
            self._api().patch_namespaced_deployment(
                plan.name,
                plan.namespace,
                body,
                dry_run="All" if dry_run else None,
                _request_timeout=WRITE_TIMEOUT_SECONDS,
            )
        except ApiException as exc:
            raise ActionError("api_error", f"写入 Deployment 失败：HTTP {exc.status}") from exc
        return {"revision": plan.target_revision, "dry_run": dry_run}

    def isolate(self, namespace: str, name: str, uid: str, version: str, token: str) -> str:
        """推进同一 Deployment 的版本，使旧回滚请求的版本条件必然失败。"""
        apps = self._api()
        try:
            target = apps.read_namespaced_deployment(
                name, namespace, _request_timeout=WRITE_TIMEOUT_SECONDS
            )
            if target.metadata.uid != uid or not target.metadata.resource_version:
                raise ActionError("identity_changed", "无法确认原 Deployment 身份")
            current_version = target.metadata.resource_version
            changes = _identity_tests(uid, current_version)
            if target.metadata.annotations is None:
                changes.append(
                    {
                        "op": "add",
                        "path": "/metadata/annotations",
                        "value": {FENCE_ANNOTATION: token},
                    }
                )
            else:
                changes.append(
                    {
                        "op": "add",
                        "path": f"/metadata/annotations/{_pointer(FENCE_ANNOTATION)}",
                        "value": token,
                    }
                )
            updated = apps.patch_namespaced_deployment(
                name, namespace, changes, _request_timeout=WRITE_TIMEOUT_SECONDS
            )
        except ApiException as exc:
            raise ActionError(
                "isolation_failed", f"Deployment 版本隔离失败：HTTP {exc.status}"
            ) from exc
        isolated = updated.metadata.resource_version
        if (
            updated.metadata.uid != uid
            or not isolated
            or isolated in {version, current_version}
            or (updated.metadata.annotations or {}).get(FENCE_ANNOTATION) != token
        ):
            raise ActionError("isolation_failed", "未确认同一 Deployment 的版本已推进")
        return isolated

    def wait_rolled_out(self, plan: RolloutPlan, timeout_seconds: float) -> bool:
        apps = self._api()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            deployment = apps.read_namespaced_deployment(
                plan.name,
                plan.namespace,
                _request_timeout=WRITE_TIMEOUT_SECONDS,
            )
            if deployment.metadata.uid != plan.deployment_uid:
                raise ActionError("identity_changed", "验证期间 Deployment 身份已变化")
            if (
                _clean_template(self._client.sanitize_for_serialization(deployment.spec.template))
                != plan.template
            ):
                raise ActionError("template_changed", "验证期间 Deployment 模板与批准目标不同")
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
        deployment = apps.read_namespaced_deployment(
            name, namespace, _request_timeout=WRITE_TIMEOUT_SECONDS
        )
        annotations = deployment.metadata.annotations or {}
        container = deployment.spec.template.spec.containers[0]
        memory_limit, memory_request = _container_memory(container)
        selector = ",".join(
            f"{key}={value}"
            for key, value in sorted((deployment.spec.selector.match_labels or {}).items())
        )
        try:
            replicasets = apps.list_namespaced_replica_set(
                namespace,
                label_selector=selector,
                _request_timeout=WRITE_TIMEOUT_SECONDS,
            ).items
            owners = {
                rs.metadata.uid
                for rs in replicasets
                if _owned_by(rs, "Deployment", deployment.metadata.uid, name=name)
            }
            pods = core.list_namespaced_pod(
                namespace,
                label_selector=selector,
                _request_timeout=WRITE_TIMEOUT_SECONDS,
            ).items
        except ApiException as exc:
            raise ActionError("api_error", f"读取工作负载状态失败：HTTP {exc.status}") from exc
        pod_states = []
        for pod in pods:
            if not any(_owned_by(pod, "ReplicaSet", uid) for uid in owners):
                continue
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
            "reconciliation_marker": annotations.get(FENCE_ANNOTATION),
            "change_cause": annotations.get(CHANGE_CAUSE),
            "image": container.image,
            "memory_limit": memory_limit,
            "memory_request": memory_request,
            "replicas": deployment.spec.replicas,
            "available_replicas": deployment.status.available_replicas,
            "pods": pod_states,
        }
