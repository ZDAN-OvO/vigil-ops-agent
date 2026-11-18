"""rollout_undo 适配器：回退到上一版本模板，并观察真实业务恢复。"""

from __future__ import annotations

import json
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.client.exceptions import ApiException
from urllib3.util import Retry

from aiops.contracts import ActionProposal

REVISION_ANNOTATION = "deployment.kubernetes.io/revision"


class ActionError(RuntimeError):
    def __init__(self, code: str, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


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
    replicaset_name: str = ""
    replicaset_uid: str = ""
    controller_uid: str = ""
    deployment: dict[str, Any] = field(default_factory=dict)

    @property
    def pinned_parameters(self) -> dict[str, Any]:
        return {
            "replicaset_name": self.replicaset_name,
            "replicaset_uid": self.replicaset_uid,
            "controller_uid": self.controller_uid,
            "target_template": self.template,
            "to_revision": self.target_revision,
        }

    @property
    def changes(self) -> list[dict]:
        return template_changes(self.deployment.get("spec", {}).get("template", {}), self.template)


def template_changes(current: dict, target: dict) -> list[dict]:
    """递归展开字典和数组；缺项以 null 表示，并保留完整的旧值或新值。"""
    changes: list[dict] = []

    def compare(before, after, path: str) -> None:
        if type(before) is type(after) and before == after:
            return
        if isinstance(before, dict) and isinstance(after, dict):
            for key in sorted(before.keys() | after.keys()):
                child = f"{path}.{key}" if path else key
                if key not in before or key not in after:
                    changes.append({"path": child, "from": before.get(key), "to": after.get(key)})
                else:
                    compare(before[key], after[key], child)
            return
        if isinstance(before, list) and isinstance(after, list):
            named = all(isinstance(item, dict) and "name" in item for item in before + after)
            for index in range(max(len(before), len(after))):
                old = before[index] if index < len(before) else None
                new = after[index] if index < len(after) else None
                left = old.get("name") if named and old else None
                right = new.get("name") if named and new else None
                label = left if left == right and left else index
                compare(old, new, f"{path}[{label}]")
            return
        changes.append({"path": path, "from": before, "to": after})

    compare(current, target, "")
    return changes


def _raw(response) -> dict:
    try:
        return json.loads(response.data)
    finally:
        response.release_conn()


def _revision(resource: dict) -> int:
    annotated = resource["metadata"].get("annotations") or {}
    return int(annotated.get(REVISION_ANNOTATION, 0))


def _controller_uid(replicaset: dict) -> str | None:
    owners = replicaset["metadata"].get("ownerReferences") or []
    controllers = [ref for ref in owners if ref.get("controller") is True]
    if len(controllers) == 1 and controllers[0].get("kind") == "Deployment":
        return controllers[0].get("uid")
    return None


def _replace_comparable(deployment: dict) -> dict:
    normalized = deepcopy(deployment)
    normalized.pop("status", None)
    for field_name in ("resourceVersion", "generation", "managedFields"):
        normalized["metadata"].pop(field_name, None)
    return normalized


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
    return {**template, "metadata": metadata}


def validate_plan(plan: RolloutPlan, proposal: ActionProposal) -> None:
    if (plan.deployment_uid, plan.resource_version) != (
        proposal.target.uid,
        proposal.target.resource_version,
    ):
        raise ActionError("drift", "审批后目标资源已变化，需重新提案")
    if (
        not plan.replicaset_uid
        or plan.controller_uid != plan.deployment_uid
        or any(
            proposal.parameters.get(key) != value for key, value in plan.pinned_parameters.items()
        )
    ):
        raise ActionError("revision_changed", "提案中的历史 ReplicaSet 或 Pod 模板已变化")


class RolloutUndoAdapter:
    def __init__(
        self,
        *,
        kubeconfig: Path | None = None,
        context_name: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._kubeconfig = kubeconfig
        self._context_name = context_name
        self._timeout = (timeout_seconds, timeout_seconds)
        self._loaded = False
        self._lock = Lock()
        self._client: k8s_client.ApiClient | None = None
        self._apps: k8s_client.AppsV1Api | None = None
        self._core: k8s_client.CoreV1Api | None = None

    def _api(self) -> k8s_client.AppsV1Api:
        with self._lock:
            if not self._loaded:
                config = k8s_client.Configuration()
                if self._kubeconfig is not None:
                    k8s_config.load_kube_config(
                        config_file=str(self._kubeconfig),
                        context=self._context_name,
                        client_configuration=config,
                    )
                else:
                    try:
                        k8s_config.load_incluster_config(client_configuration=config)
                    except k8s_config.ConfigException:
                        k8s_config.load_kube_config(
                            context=self._context_name, client_configuration=config
                        )
                # 禁止连接重试、状态码重试和重定向导致的第二次真实写入。
                config.retries = Retry(total=False, redirect=False)
                self._client = k8s_client.ApiClient(config)
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

    def _deployment(self, namespace: str, name: str) -> dict:
        try:
            return _raw(
                self._api().read_namespaced_deployment(
                    name, namespace, _preload_content=False, _request_timeout=self._timeout
                )
            )
        except ApiException as exc:
            code = "not_found" if exc.status == 404 else "api_error"
            raise ActionError(code, f"读取 Deployment 失败：HTTP {exc.status}") from exc

    def plan(self, namespace: str, name: str) -> RolloutPlan:
        """按版本与控制器归属选定历史模板，保留 API 的原始 JSON。"""
        deployment = self._deployment(namespace, name)
        revision = _revision(deployment)
        uid = deployment["metadata"]["uid"]
        selector = deployment["spec"]["selector"]
        clauses = [
            f"{key}={value}" for key, value in sorted(selector.get("matchLabels", {}).items())
        ]
        for rule in selector.get("matchExpressions", []):
            key, operator = rule["key"], rule["operator"]
            if operator in {"In", "NotIn"}:
                op = "in" if operator == "In" else "notin"
                clauses.append(f"{key} {op} ({','.join(rule['values'])})")
            elif operator == "Exists":
                clauses.append(key)
            elif operator == "DoesNotExist":
                clauses.append(f"!{key}")
        try:
            result = _raw(
                self._api().list_namespaced_replica_set(
                    namespace,
                    label_selector=",".join(clauses),
                    _preload_content=False,
                    _request_timeout=self._timeout,
                )
            )
        except ApiException as exc:
            raise ActionError("api_error", f"读取 ReplicaSet 失败：HTTP {exc.status}") from exc
        older = [
            item
            for item in result["items"]
            if _controller_uid(item) == uid and 0 < _revision(item) < revision
        ]
        if not older:
            raise ActionError("no_previous_revision", "没有可回退的历史 revision")
        latest = max(map(_revision, older))
        candidates = [item for item in older if _revision(item) == latest]
        if len(candidates) != 1:
            raise ActionError("revision_changed", "历史 revision 对应多个 ReplicaSet")
        plan = self._make_plan(deployment, candidates[0], namespace, name)
        if not plan.changes:
            raise ActionError("same_template", "上一 revision 的 Pod 模板与当前相同")
        return plan

    def _make_plan(
        self, deployment: dict, replicaset: dict, namespace: str, name: str
    ) -> RolloutPlan:
        current = deployment["spec"]["template"]
        target = _clean_template(replicaset["spec"]["template"])
        current_container = current["spec"]["containers"][0]
        target_container = target["spec"]["containers"][0]

        def memory(container: dict, group: str) -> str | None:
            return ((container.get("resources") or {}).get(group) or {}).get("memory")

        return RolloutPlan(
            namespace=namespace,
            name=name,
            current_revision=_revision(deployment),
            target_revision=_revision(replicaset),
            current_image=current_container["image"],
            target_image=target_container["image"],
            template=target,
            deployment_uid=deployment["metadata"]["uid"],
            resource_version=deployment["metadata"]["resourceVersion"],
            current_memory_limit=memory(current_container, "limits"),
            target_memory_limit=memory(target_container, "limits"),
            current_memory_request=memory(current_container, "requests"),
            target_memory_request=memory(target_container, "requests"),
            replicaset_name=replicaset["metadata"]["name"],
            replicaset_uid=replicaset["metadata"]["uid"],
            controller_uid=_controller_uid(replicaset) or "",
            deployment=deployment,
        )

    def recheck(self, proposal: ActionProposal) -> RolloutPlan:
        target = proposal.target
        expected_rs = proposal.parameters.get("replicaset_name")
        if not expected_rs:
            raise ActionError("revision_changed", "提案未固定历史模板，请重新提案")
        deployment = self._deployment(target.namespace, target.name)
        if (deployment["metadata"]["uid"], deployment["metadata"]["resourceVersion"]) != (
            target.uid,
            target.resource_version,
        ):
            raise ActionError("drift", "审批后目标资源已变化，需重新提案")
        try:
            replicas = _raw(
                self._api().read_namespaced_replica_set(
                    expected_rs,
                    target.namespace,
                    _preload_content=False,
                    _request_timeout=self._timeout,
                )
            )
        except ApiException as exc:
            code = "revision_changed" if exc.status == 404 else "api_error"
            raise ActionError(code, f"读取提案中的 ReplicaSet 失败：HTTP {exc.status}") from exc
        plan = self._make_plan(deployment, replicas, target.namespace, target.name)
        validate_plan(plan, proposal)
        return plan

    def apply(
        self,
        plan: RolloutPlan,
        *,
        dry_run: bool,
        proposal_id: str,
        expires_at: datetime | None = None,
    ) -> dict[str, Any]:
        """用 resourceVersion 保护模板替换，其他 Deployment 字段原样提交。"""
        apps = self._api()
        body = deepcopy(plan.deployment)
        body["spec"]["template"] = plan.template
        if not dry_run and expires_at is not None and datetime.now(UTC) >= expires_at:
            raise ActionError("expired", "授权已过期，未发写请求")
        try:
            response = apps.replace_namespaced_deployment(
                plan.name,
                plan.namespace,
                body,
                dry_run="All" if dry_run else None,
                _preload_content=False,
                _request_timeout=self._timeout,
            )
        except ApiException as exc:
            raise ActionError(
                "api_error", f"替换 Deployment 失败：HTTP {exc.status}", http_status=exc.status
            ) from exc
        if not dry_run:
            response.close()
            response.release_conn()
        elif _replace_comparable(_raw(response)) != _replace_comparable(body):
            raise ActionError("drift", "预演结果与提案模板或原 Deployment 配置不一致")
        return {"revision": plan.target_revision, "dry_run": dry_run}

    def template_matches(self, plan: RolloutPlan) -> bool:
        deployment = self._deployment(plan.namespace, plan.name)
        return (
            deployment["metadata"]["uid"] == plan.deployment_uid
            and deployment["spec"]["template"] == plan.template
        )

    def wait_rolled_out(self, plan: RolloutPlan, timeout_seconds: float) -> bool:
        apps = self._api()
        deadline = time.monotonic() + timeout_seconds
        while (remaining := deadline - time.monotonic()) > 0:
            # 单次读取也受剩余时限约束；连接挂起时抛出异常，由执行器按"已写入、待核实"收尾。
            per_call = min(self._timeout[1], remaining)
            deployment = apps.read_namespaced_deployment(
                plan.name, plan.namespace, _request_timeout=(per_call, per_call)
            )
            status = deployment.status
            replicas = deployment.spec.replicas or 1
            if (
                deployment.metadata.generation == status.observed_generation
                and (status.updated_replicas or 0) == replicas
                and (status.available_replicas or 0) == replicas
                and (status.unavailable_replicas or 0) == 0
            ):
                return True
            time.sleep(max(0.0, min(2.0, deadline - time.monotonic())))
        return False

    def state(self, namespace: str, name: str) -> dict[str, Any]:
        apps = self._api()
        core = self._core_api()
        deployment = apps.read_namespaced_deployment(
            name, namespace, _request_timeout=self._timeout
        )
        annotations = deployment.metadata.annotations or {}
        container = deployment.spec.template.spec.containers[0]
        memory_limit, memory_request = _container_memory(container)
        selector = ",".join(
            f"{key}={value}"
            for key, value in sorted((deployment.spec.selector.match_labels or {}).items())
        )
        try:
            pods = core.list_namespaced_pod(
                namespace, label_selector=selector, _request_timeout=self._timeout
            ).items
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
