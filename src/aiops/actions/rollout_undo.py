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
                self._loaded = True
        if self._apps is None or self._client is None:
            raise ActionError("client_error", "Kubernetes 客户端初始化失败")
        return self._apps

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

        current_image = deployment.spec.template.spec.containers[0].image
        target_image = replicaset.spec.template.spec.containers[0].image
        if target_image == current_image:
            raise ActionError("same_template", "上一 revision 的镜像与当前相同，回退没有意义")

        return RolloutPlan(
            namespace=namespace,
            name=name,
            current_revision=current_revision,
            target_revision=target_revision,
            current_image=current_image,
            target_image=target_image,
            template=_clean_template(
                self._client.sanitize_for_serialization(replicaset.spec.template)
            ),
            deployment_uid=deployment.metadata.uid,
            resource_version=deployment.metadata.resource_version,
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
        deployment = apps.read_namespaced_deployment(name, namespace)
        annotations = deployment.metadata.annotations or {}
        return {
            "uid": deployment.metadata.uid,
            "resource_version": deployment.metadata.resource_version,
            "revision": int(annotations.get(REVISION_ANNOTATION, "1")),
            "image": deployment.spec.template.spec.containers[0].image,
            "replicas": deployment.spec.replicas,
            "available_replicas": deployment.status.available_replicas,
        }
