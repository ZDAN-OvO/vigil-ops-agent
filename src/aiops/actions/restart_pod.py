"""restart_pod 适配器：删除指定 Pod，由控制器重建，并用真实请求验证恢复。

不提供 undo：原 Pod 删除后无法恢复，只能用新的 Pod 继续验证。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.client.exceptions import ApiException

from aiops.actions.rollout_undo import ActionError

DISCARDABLE_LABEL = "vigil.io/discardable"
VOLATILE_POD_SPEC_FIELDS = ("nodeName",)


@dataclass(frozen=True)
class PodPlan:
    namespace: str
    name: str
    uid: str
    resource_version: str
    owner_kind: str | None
    owner_name: str | None
    owner_uid: str | None
    template_hash: str
    spec_hash: str
    sibling_uids: tuple[str, ...]
    restart_count: int
    ready: bool
    image: str
    discardable: bool


def _hash(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def pod_spec_hash(spec: dict[str, Any]) -> str:
    """Pod 规格去掉随 Pod 变化的字段后取哈希，用来确认重建用的模板没有变化。

    排除项：nodeName（调度位置）和 kube-api-access-* 投影卷（名字带随机后缀）。
    """
    cleaned = {key: value for key, value in spec.items() if key not in VOLATILE_POD_SPEC_FIELDS}
    token_volumes = {
        volume.get("name")
        for volume in cleaned.get("volumes") or []
        if str(volume.get("name", "")).startswith("kube-api-access-")
    }
    if token_volumes:
        cleaned["volumes"] = [
            volume for volume in cleaned["volumes"] if volume.get("name") not in token_volumes
        ]
        containers = []
        for container in cleaned.get("containers") or []:
            mounts = [
                mount
                for mount in container.get("volumeMounts") or []
                if mount.get("name") not in token_volumes
            ]
            containers.append({**container, "volumeMounts": mounts})
        cleaned["containers"] = containers
    return _hash(cleaned)


class PodRestartAdapter:
    def __init__(self, *, kubeconfig: Path | None = None, context_name: str | None = None) -> None:
        self._kubeconfig = kubeconfig
        self._context_name = context_name
        self._loaded = False
        self._lock = Lock()
        self._client: k8s_client.ApiClient | None = None
        self._core: k8s_client.CoreV1Api | None = None
        self._apps: k8s_client.AppsV1Api | None = None

    def _api(self) -> k8s_client.CoreV1Api:
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
                self._core = k8s_client.CoreV1Api(self._client)
                self._apps = k8s_client.AppsV1Api(self._client)
                self._loaded = True
        if self._core is None or self._client is None:
            raise ActionError("client_error", "Kubernetes 客户端初始化失败")
        return self._core

    def _serialize(self, item: Any) -> dict[str, Any]:
        self._api()
        if self._client is None:
            raise ActionError("client_error", "Kubernetes 客户端初始化失败")
        return self._client.sanitize_for_serialization(item)

    def _owner(self, pod: Any) -> Any | None:
        for owner in pod.metadata.owner_references or []:
            if owner.controller:
                return owner
        return None

    def plan(self, namespace: str, name: str) -> PodPlan:
        """读取目标 Pod 的身份、归属和模板；任何读不到的字段都不拿空值顶替。"""
        core = self._api()
        try:
            pod = core.read_namespaced_pod(name, namespace)
        except ApiException as exc:
            code = "not_found" if exc.status == 404 else "api_error"
            raise ActionError(code, f"读取 Pod 失败：HTTP {exc.status}") from exc

        owner = self._owner(pod)
        labels = pod.metadata.labels or {}
        statuses = pod.status.container_statuses or []
        restarts = sum(int(status.restart_count or 0) for status in statuses)
        ready = bool(statuses) and all(bool(status.ready) for status in statuses)
        image = pod.spec.containers[0].image if pod.spec.containers else ""

        template_hash = ""
        if owner is not None and owner.kind == "ReplicaSet" and self._apps is not None:
            try:
                replicaset = self._apps.read_namespaced_replica_set(owner.name, namespace)
                template_hash = _hash(
                    self._serialize(replicaset.spec.template) if replicaset.spec.template else {}
                )
            except ApiException as exc:
                raise ActionError("api_error", f"读取 ReplicaSet 失败：HTTP {exc.status}") from exc
        siblings: tuple[str, ...] = ()
        if owner is not None and owner.uid:
            try:
                siblings = tuple(
                    pod["uid"] for pod in self.pods_of_owner(namespace, owner.uid) if pod["uid"]
                )
            except ActionError:
                siblings = ()
        return PodPlan(
            namespace=namespace,
            name=pod.metadata.name,
            uid=pod.metadata.uid,
            resource_version=pod.metadata.resource_version,
            owner_kind=owner.kind if owner else None,
            owner_name=owner.name if owner else None,
            owner_uid=owner.uid if owner else None,
            template_hash=template_hash or pod_spec_hash(self._serialize(pod.spec)),
            spec_hash=pod_spec_hash(self._serialize(pod.spec)),
            sibling_uids=siblings,
            restart_count=restarts,
            ready=ready,
            image=image,
            discardable=labels.get(DISCARDABLE_LABEL) == "true",
        )

    def _delete_options(self, plan: PodPlan) -> k8s_client.V1DeleteOptions:
        """只用 UID 作前置条件。Pod 的 resourceVersion 随状态更新不断变化，
        绑定它会让删除在读取后几毫秒内就 409，无法用于正常运行的 Pod。"""
        return k8s_client.V1DeleteOptions(preconditions=k8s_client.V1Preconditions(uid=plan.uid))

    def dry_run(self, plan: PodPlan) -> dict[str, Any]:
        """带 UID 前置条件的删除试运行，由 API 服务器校验身份和权限。"""
        core = self._api()
        try:
            core.delete_namespaced_pod(
                plan.name,
                plan.namespace,
                body=self._delete_options(plan),
                dry_run="All",
            )
        except ApiException as exc:
            code = "precondition_failed" if exc.status == 409 else "api_error"
            raise ActionError(code, f"dry-run 删除失败：HTTP {exc.status}") from exc
        return {"dry_run": True, "uid": plan.uid}

    def execute(self, plan: PodPlan) -> dict[str, Any]:
        """删除目标 Pod；前置条件保证删的是审批时看到的那个对象。"""
        core = self._api()
        try:
            core.delete_namespaced_pod(plan.name, plan.namespace, body=self._delete_options(plan))
        except ApiException as exc:
            code = "precondition_failed" if exc.status == 409 else "api_error"
            raise ActionError(code, f"删除 Pod 失败：HTTP {exc.status}") from exc
        return {"deleted": plan.name, "uid": plan.uid}

    def pods_of_owner(self, namespace: str, owner_uid: str) -> list[dict[str, Any]]:
        core = self._api()
        try:
            pods = core.list_namespaced_pod(namespace).items
        except ApiException as exc:
            raise ActionError("api_error", f"读取 Pod 列表失败：HTTP {exc.status}") from exc
        result = []
        for pod in pods:
            owner = self._owner(pod)
            if owner is None or owner.uid != owner_uid:
                continue
            serialized = self._serialize(pod.spec)
            statuses = pod.status.container_statuses or []
            result.append(
                {
                    "name": pod.metadata.name,
                    "uid": pod.metadata.uid,
                    "ready": bool(statuses) and all(bool(s.ready) for s in statuses),
                    "restarts": sum(int(s.restart_count or 0) for s in statuses),
                    "template_hash": pod_spec_hash(serialized),
                    "ip": pod.status.pod_ip,
                    "phase": pod.status.phase,
                }
            )
        return result

    def wait_rebuilt(
        self, plan: PodPlan, timeout_seconds: float, sample_seconds: float = 2.0
    ) -> dict[str, Any] | None:
        """等控制器生成新的就绪 Pod；旧 UID 的 Pod 不能算作重建结果。"""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            for pod in self.pods_of_owner(plan.namespace, plan.owner_uid or ""):
                # 只认删除之后新出现的 UID。操作前就存在的兄弟副本不能算重建结果。
                if pod["uid"] in plan.sibling_uids or pod["uid"] == plan.uid:
                    continue
                if pod["ready"]:
                    return pod
            time.sleep(sample_seconds)
        return None

    def pod_state(self, namespace: str, owner_uid: str, uid: str) -> dict[str, Any] | None:
        """按 UID 重读 Pod，用于观察窗结束后确认就绪和重启次数。"""
        for pod in self.pods_of_owner(namespace, owner_uid):
            if pod["uid"] == uid:
                return pod
        return None

    def state(self, namespace: str, owner_uid: str) -> list[dict[str, Any]]:
        return self.pods_of_owner(namespace, owner_uid)

    def pod_get(self, namespace: str, name: str, path: str) -> dict[str, Any]:
        """经 API 服务器代理向 Pod 发真实 HTTP 请求；k3d 里 apiserver 到 ClusterIP 不通，
        所以业务检查直接打到重建后的 Pod。"""
        core = self._api()
        try:
            body = core.connect_get_namespaced_pod_proxy_with_path(
                name=name, namespace=namespace, path=path.lstrip("/")
            )
        except ApiException as exc:
            return {"ok": False, "status": exc.status, "body": None}
        except Exception as exc:  # noqa: BLE001 - 连接失败按未恢复记录
            return {"ok": False, "status": None, "body": exc.__class__.__name__}
        return {"ok": True, "status": 200, "body": body}

    def service_selects(self, namespace: str, service: str, pod_ip: str | None) -> dict[str, Any]:
        """确认 Service 的后端里有新 Pod，避免出现重建成功但流量仍打不到的情况。"""
        core = self._api()
        try:
            endpoints = core.read_namespaced_endpoints(service, namespace)
        except ApiException as exc:
            return {"ok": False, "status": exc.status, "pod_ip": pod_ip}
        addresses = sorted(
            address.ip
            for subset in endpoints.subsets or []
            for address in subset.addresses or []
            if address.ip
        )
        return {
            "ok": bool(pod_ip) and pod_ip in addresses,
            "pod_ip": pod_ip,
            "addresses": addresses,
        }
