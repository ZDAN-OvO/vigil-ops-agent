"""Kubernetes 受控变更的取证、条件写入和核实。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, DecimalException
from functools import partial
from pathlib import Path
from typing import Any, Literal

from kubernetes.client import ApiClient
from kubernetes.client.api.admissionregistration_v1_api import AdmissionregistrationV1Api
from kubernetes.client.exceptions import ApiException
from kubernetes.utils.quantity import parse_quantity

from aiops.actions.kubernetes_write import WRITE_TIMEOUT_SECONDS, action_client
from aiops.contracts import ActionTarget

RECONCILIATION_ANNOTATION = "vigil.aiops/reconciliation"
_WORKLOADS = {"Deployment", "StatefulSet"}
_RESOURCES = {
    "Deployment": ("apps/v1", "deployments"),
    "StatefulSet": ("apps/v1", "statefulsets"),
    "ReplicaSet": ("apps/v1", "replicasets"),
    "DaemonSet": ("apps/v1", "daemonsets"),
    "ReplicationController": ("v1", "replicationcontrollers"),
    "Pod": ("v1", "pods"),
    "PodTemplate": ("v1", "podtemplates"),
    "Job": ("batch/v1", "jobs"),
    "CronJob": ("batch/v1", "cronjobs"),
    "ConfigMap": ("v1", "configmaps"),
}
_RESOURCE_FIELDS = {
    "request_cpu": ("requests", "cpu"),
    "request_memory": ("requests", "memory"),
    "limit_cpu": ("limits", "cpu"),
    "limit_memory": ("limits", "memory"),
}
_QUANTITY = re.compile(
    r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3}|[KMGTPE]i|[numkMGTPE])?"
)
_LABEL_KEY = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?")
_LABEL_VALUE = re.compile(r"[A-Za-z0-9_.-]*")
_REJECTED = {400, 401, 403, 404, 405, 409, 410, 413, 415, 422, 429}

type Scalar = str | int | None
type Document = dict[str, Any]
type FieldPath = tuple[str | int, ...]
type Registration = Callable[[ActionTarget], bool]
type Exclusivity = Callable[[ActionTarget, ActionTarget], bool]


class RemediationKubeError(RuntimeError):
    def __init__(self, code: str, status: int | None = None) -> None:
        self.code = code
        self.status = status
        super().__init__(code if status is None else f"{code} (HTTP {status})")


class WriteRejected(RemediationKubeError):
    """服务端已明确拒绝补丁。"""


class WriteUnknown(RemediationKubeError):
    """可能已经写入，需要隔离版本并核实。"""


@dataclass(frozen=True, repr=False)
class Snapshot:
    workload: ActionTarget
    object: ActionTarget
    kind: str
    parameters: dict[str, Scalar]
    fields: dict[str, Scalar]
    current: dict[str, Scalar]
    absent_env_value: bool = False

    def to_dict(self) -> Document:
        return {
            "workload": self.workload.model_dump(mode="json"),
            "object": self.object.model_dump(mode="json"),
            "kind": self.kind,
            "parameters": dict(self.parameters),
            "fields": dict(self.fields),
            "current": dict(self.current),
            "absent_env_value": self.absent_env_value,
        }

    @classmethod
    def from_dict(cls, data: Document) -> Snapshot:
        return cls(
            ActionTarget.model_validate(data["workload"]),
            ActionTarget.model_validate(data["object"]),
            data["kind"],
            dict(data["parameters"]),
            dict(data["fields"]),
            dict(data["current"]),
            data["absent_env_value"],
        )


@dataclass(frozen=True, repr=False)
class PatchPlan:
    before: Snapshot
    after: dict[str, Scalar]
    body_json: str
    expected_spec_digest: str
    created_resource_maps: tuple[tuple[str, ...], ...] = ()
    restore: bool = False

    def to_dict(self) -> Document:
        return {
            "before": self.before.to_dict(),
            "after": dict(self.after),
            "body_json": self.body_json,
            "expected_spec_digest": self.expected_spec_digest,
            "created_resource_maps": [list(path) for path in self.created_resource_maps],
            "restore": self.restore,
        }

    @classmethod
    def from_dict(cls, data: Document) -> PatchPlan:
        return cls(
            Snapshot.from_dict(data["before"]),
            dict(data["after"]),
            data["body_json"],
            data["expected_spec_digest"],
            tuple(tuple(path) for path in data["created_resource_maps"]),
            data["restore"],
        )


@dataclass(frozen=True)
class Preflight:
    plan: PatchPlan = field(repr=False)
    _seal: object = field(repr=False, compare=False)


@dataclass(frozen=True)
class WriteReceipt:
    object: ActionTarget
    generation: int | None

    def to_dict(self) -> Document:
        return {"object": self.object.model_dump(mode="json"), "generation": self.generation}

    @classmethod
    def from_dict(cls, data: Document) -> WriteReceipt:
        return cls(ActionTarget.model_validate(data["object"]), data["generation"])


@dataclass(frozen=True)
class Verification:
    state: Literal["pending", "ready", "drift"]


@dataclass(frozen=True)
class ObservedPod:
    name: str
    uid: str
    container_id: str
    restarts: int
    oom_finished_at: tuple[datetime, ...]


@dataclass(frozen=True)
class Inspection:
    state: Literal["before", "after", "drift"]
    object: ActionTarget
    fields: dict[str, Scalar] = field(repr=False)


@dataclass(frozen=True)
class PodEvidence:
    workload_uid: str
    replica_set_uid: str | None
    pod_name: str
    pod_uid: str
    container: str
    finished_at: datetime
    restart_count: int
    container_id: str | None
    reason: str = "OOMKilled"


def _dict(value: Any) -> Document:
    if not isinstance(value, dict):
        raise RemediationKubeError("invalid_object")
    return value


def _list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise RemediationKubeError("invalid_object")
    return value


def _string(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise RemediationKubeError("invalid_object")
    return value


def _integer(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise RemediationKubeError("invalid_object")
    return value


def _quantity(value: Any) -> Decimal:
    if not isinstance(value, str) or len(value) > 96 or not _QUANTITY.fullmatch(value):
        raise RemediationKubeError("invalid_quantity")
    try:
        result = parse_quantity(value)
    except (ValueError, DecimalException):
        raise RemediationKubeError("invalid_quantity") from None
    if not result.is_finite() or result < 0:
        raise RemediationKubeError("invalid_quantity")
    return result


def _same(key: str, before: Scalar, after: Scalar) -> bool:
    if key in _RESOURCE_FIELDS and before is not None and after is not None:
        return _quantity(before) == _quantity(after)
    return before == after


def _same_fields(before: Mapping[str, Scalar], after: Mapping[str, Scalar]) -> bool:
    return before.keys() == after.keys() and all(
        _same(key, value, after[key]) for key, value in before.items()
    )


def _parameters(kind: str, parameters: Mapping[str, Any]) -> dict[str, Scalar]:
    if not isinstance(parameters, Mapping):
        raise RemediationKubeError("invalid_parameters")
    result = dict(parameters)
    keys = set(result)
    if kind == "scale_replicas":
        replicas = result.get("replicas")
        valid = keys == {"replicas"} and type(replicas) is int and 0 < replicas <= 2**31 - 1
    elif kind == "adjust_resources":
        names = keys - {"container"}
        container = result.get("container")
        valid = (
            isinstance(container, str)
            and bool(container)
            and bool(names)
            and names <= _RESOURCE_FIELDS.keys()
        )
        if valid:
            for name in names:
                _quantity(result[name])
    elif kind == "update_config":
        source = result.get("source")
        source_key = None
        if isinstance(source, str):
            source_key = {"env": "container", "config_map": "config_map"}.get(source)
        valid = source_key is not None and keys == {"source", "key", "value", source_key}
        if valid:
            valid = all(isinstance(result[key], str) and result[key] for key in ("key", source_key))
            valid = valid and isinstance(result["value"], str)
    else:
        valid = False
    if not valid:
        raise RemediationKubeError("invalid_parameters")
    return result


def _container(spec: Document, name: Scalar) -> tuple[int, Document]:
    matches = [
        (index, item)
        for index, item in enumerate(_list(spec.get("containers")))
        if _dict(item).get("name") == name
    ]
    if len(matches) != 1:
        raise RemediationKubeError("container_unavailable")
    return matches[0]


def _optional(parent: Document, key: str) -> Document:
    return _dict(parent[key]) if key in parent else {}


def _template(raw: Document) -> Document:
    return _dict(_dict(raw.get("spec")).get("template"))


def _fields(
    kind: str, parameters: Mapping[str, Scalar], raw: Document
) -> tuple[dict[str, Scalar], dict[str, FieldPath]]:
    if kind == "scale_replicas":
        value = _integer(_dict(raw.get("spec")).get("replicas"))
        return {"replicas": value}, {"replicas": ("spec", "replicas")}
    if parameters.get("source") == "config_map":
        key = _string(parameters["key"])
        value = _dict(raw.get("data")).get(key)
        if not isinstance(value, str):
            raise RemediationKubeError("config_value_unavailable")
        return {"value": value}, {"value": ("data", key)}

    index, container = _container(_dict(_template(raw).get("spec")), parameters["container"])
    prefix = ("spec", "template", "spec", "containers", index)
    if kind == "update_config":
        matches = [
            (index, item)
            for index, item in enumerate(_list(container.get("env", [])))
            if _dict(item).get("name") == parameters["key"]
        ]
        if len(matches) != 1:
            raise RemediationKubeError("config_value_unavailable")
        env_index, entry = matches[0]
        # Kubernetes 省略空字符串的 value。
        value = entry.get("value", "")
        if "valueFrom" in entry or not isinstance(value, str):
            raise RemediationKubeError("config_value_unavailable")
        return {"value": value}, {"value": (*prefix, "env", env_index, "value")}

    values: dict[str, Scalar] = {}
    paths: dict[str, FieldPath] = {}
    resources = _optional(container, "resources")
    for key, (group, resource) in _RESOURCE_FIELDS.items():
        if key in parameters:
            amounts = _optional(resources, group)
            values[key] = amounts.get(resource)
            if resource in amounts:
                _quantity(values[key])
            paths[key] = (*prefix, "resources", group, resource)
    return values, paths


def _pointer(path: FieldPath) -> str:
    return "/" + "/".join(str(segment).replace("~", "~0").replace("/", "~1") for segment in path)


def _identity_tests(target: ActionTarget) -> list[Document]:
    return [
        {"op": "test", "path": "/metadata/uid", "value": target.uid},
        {"op": "test", "path": "/metadata/resourceVersion", "value": target.resource_version},
    ]


def _path_value(document: Document, path: FieldPath, default: Any = None) -> Any:
    value: Any = document
    for segment in path:
        try:
            value = value[segment]
        except (KeyError, IndexError):
            return default
    return value


def _patch_value(
    current: Document, path: FieldPath, value: Scalar, operations: list[Document]
) -> list[FieldPath]:
    created = []
    parent: Any = current
    for index, segment in enumerate(path[:-1], start=1):
        if isinstance(parent, dict) and segment not in parent:
            parent[segment] = {}
            created.append(path[:index])
            operations.append({"op": "add", "path": _pointer(path[:index]), "value": {}})
        parent = parent[segment]
    key = path[-1]
    if value is None:
        del parent[key]
        operations.append({"op": "remove", "path": _pointer(path)})
    else:
        op = "replace" if key in parent else "add"
        parent[key] = value
        operations.append({"op": op, "path": _pointer(path), "value": value})
    return created


def _digest(document: Document) -> str:
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _normalize_pod_spec(spec: Document) -> None:
    for group in ("containers", "initContainers", "ephemeralContainers"):
        for container in _list(spec.get(group, [])):
            resources = _optional(_dict(container), "resources")
            for field_name in ("requests", "limits"):
                amounts = _optional(resources, field_name)
                if not amounts:
                    resources.pop(field_name, None)
                for resource, amount in amounts.items():
                    amounts[resource] = format(_quantity(amount).normalize(), "f")
            if not resources:
                container.pop("resources", None)
            for entry in _list(container.get("env", [])):
                if _dict(entry).get("value") == "":
                    entry.pop("value")


def _spec_digest(raw: Document) -> str:
    spec = deepcopy(_dict(raw.get("spec")))
    _normalize_pod_spec(_dict(_dict(spec.get("template")).get("spec")))
    return _digest(spec)


def _rs_template_digest(raw: Document) -> str:
    template = deepcopy(_template(raw))
    labels = _optional(_dict(template.get("metadata")), "labels")
    labels.pop("pod-template-hash", None)
    _normalize_pod_spec(_dict(template.get("spec")))
    return _digest(template)


def _owner(raw: Document) -> Document:
    refs = _list(_dict(raw.get("metadata")).get("ownerReferences", []))
    controlled = [_dict(ref) for ref in refs if _dict(ref).get("controller") is True]
    if len(controlled) > 1:
        raise RemediationKubeError("owner_unavailable")
    return controlled[0] if controlled else {}


def _owned_by(raw: Document, kind: str, name: str, uid: str) -> bool:
    owner = _owner(raw)
    return (
        bool(uid)
        and owner.get("apiVersion") == _RESOURCES[kind][0]
        and owner.get("kind") == kind
        and owner.get("name") == name
        and owner.get("uid") == uid
    )


def _pod_spec(kind: str, raw: Document) -> Document:
    if kind == "Pod":
        return _dict(raw.get("spec"))
    if kind == "PodTemplate":
        return _dict(_dict(raw.get("template")).get("spec"))
    if kind == "CronJob":
        raw = _dict(_dict(raw.get("spec")).get("jobTemplate"))
    if kind == "ReplicationController" and _dict(raw.get("spec")).get("template") is None:
        return {}
    return _dict(_template(raw).get("spec"))


def _config_map_reference(spec: Document, name: str) -> bool:
    for volume in _list(spec.get("volumes", [])):
        volume = _dict(volume)
        if _optional(volume, "configMap").get("name") == name:
            return True
        for source in _list(_optional(volume, "projected").get("sources", [])):
            if _optional(_dict(source), "configMap").get("name") == name:
                return True
    for group in ("containers", "initContainers", "ephemeralContainers"):
        for container in _list(spec.get(group, [])):
            container = _dict(container)
            for entry in _list(container.get("envFrom", [])):
                if _optional(_dict(entry), "configMapRef").get("name") == name:
                    return True
            for entry in _list(container.get("env", [])):
                if (
                    _optional(_optional(_dict(entry), "valueFrom"), "configMapKeyRef").get("name")
                    == name
                ):
                    return True
    return False


def _resource_path(kind: str, *, named: bool) -> str:
    api_version, plural = _RESOURCES[kind]
    prefix = "/api" if api_version == "v1" else "/apis"
    path = f"{prefix}/{api_version}/namespaces/{{namespace}}/{plural}"
    return path + "/{name}" if named else path


class RemediationKubeAdapter:
    def __init__(
        self,
        *,
        read_client: ApiClient,
        write_client: ApiClient,
        is_registered: Registration,
        config_map_exclusive: Exclusivity | None = None,
    ) -> None:
        self._read = read_client
        self._write = write_client
        self._registered = is_registered
        self._exclusive = config_map_exclusive
        self._seal = object()
        for api in (read_client, write_client):
            api.configuration.debug = False
        pool = write_client.rest_client.pool_manager
        pool.request = partial(pool.request, retries=False, redirect=False)

    def require_admission(self, version: str, *, exclusive_config: bool) -> None:
        """专用身份还必须能确认与策略同版本的拒绝准入已部署。"""
        api = AdmissionregistrationV1Api(self._write)
        names = ["vigil-remediation-write"]
        if exclusive_config:
            names.append("vigil-remediation-config-consumers")
        for name in names:
            try:
                policy = api.read_validating_admission_policy(
                    name, _request_timeout=WRITE_TIMEOUT_SECONDS
                )
                binding = api.read_validating_admission_policy_binding(
                    name,
                    _request_timeout=WRITE_TIMEOUT_SECONDS,
                )
                checked = policy.status and policy.status.type_checking
                if (
                    not checked
                    or checked.expression_warnings
                    or (policy.metadata.annotations or {}).get("vigil.aiops/policy-version")
                    != version
                    or policy.spec.failure_policy != "Fail"
                    or binding.spec.policy_name != name
                    or "Deny" not in binding.spec.validation_actions
                    or binding.spec.match_resources is not None
                ):
                    raise RemediationKubeError("admission_policy_unverified")
            except (AttributeError, ApiException):
                raise RemediationKubeError("admission_policy_unverified") from None

    @classmethod
    def from_kubeconfig(
        cls,
        *,
        kubeconfig: Path,
        context_name: str | None = None,
        read_client: ApiClient | None = None,
        is_registered: Registration,
        config_map_exclusive: Exclusivity | None = None,
    ) -> RemediationKubeAdapter:
        if kubeconfig is None:
            raise RemediationKubeError("write_kubeconfig_required")
        try:
            write_client = action_client(kubeconfig, context_name)
        except Exception:
            raise RemediationKubeError("write_client_unavailable") from None
        return cls(
            read_client=read_client if read_client is not None else write_client,
            write_client=write_client,
            is_registered=is_registered,
            config_map_exclusive=config_map_exclusive,
        )

    def _request(
        self,
        method: str,
        kind: str,
        namespace: str,
        *,
        name: str | None = None,
        query: list[tuple[str, str]] | None = None,
        body: list[Document] | None = None,
        real_write: bool = False,
    ) -> Document:
        api = self._write if method == "PATCH" else self._read
        params = {"namespace": namespace}
        headers = {"Accept": "application/json"}
        if name is not None:
            params["name"] = name
        if body is not None:
            headers["Content-Type"] = "application/json-patch+json"
        try:
            response = api.call_api(
                _resource_path(kind, named=name is not None),
                method,
                path_params=params,
                query_params=query,
                header_params=headers,
                body=body,
                auth_settings=["BearerToken"],
                _preload_content=False,
                _return_http_data_only=True,
                _request_timeout=WRITE_TIMEOUT_SECONDS,
            )
            try:
                return _dict(json.loads(response.data))
            finally:
                response.close()
        except ApiException as exc:
            status = exc.status if type(exc.status) is int else None
            if method == "PATCH" and status in _REJECTED:
                raise WriteRejected("write_rejected", status) from None
            if real_write:
                raise WriteUnknown("write_unknown", status) from None
            raise RemediationKubeError("api_error", status) from None
        except (Exception, asyncio.CancelledError):
            if real_write:
                raise WriteUnknown("write_unknown") from None
            raise RemediationKubeError("api_error") from None

    def _raw(self, kind: str, namespace: str, name: str) -> Document:
        return self._request("GET", kind, namespace, name=name)

    def _items(self, kind: str, namespace: str, selector: str = "") -> list[Document]:
        query = [("limit", "250")]
        if selector:
            query.append(("labelSelector", selector))
        items: list[Document] = []
        seen: set[str] = set()
        version = None
        continuation = ""
        for _ in range(40):
            page_query = [*query, ("continue", continuation)] if continuation else query
            page = self._request("GET", kind, namespace, query=page_query)
            metadata = _dict(page.get("metadata"))
            page_version = _string(metadata.get("resourceVersion"))
            if page.get("kind") != kind + "List" or (
                version is not None and page_version != version
            ):
                raise RemediationKubeError("incomplete_list")
            version = page_version
            for raw in _list(page.get("items")):
                raw = _dict(raw)
                if _dict(raw.get("metadata")).get("namespace") != namespace:
                    raise RemediationKubeError("incomplete_list")
                items.append(raw)
            continuation = metadata.get("continue", "")
            if continuation == "":
                return items
            if not isinstance(continuation, str) or continuation in seen:
                raise RemediationKubeError("incomplete_list")
            seen.add(continuation)
        raise RemediationKubeError("incomplete_list")

    def _register(self, target: ActionTarget, kinds: set[str], *, resolving: bool = False) -> None:
        if (
            target.kind not in kinds
            or not all((target.cluster, target.namespace, target.name))
            or not (resolving or target.uid)
        ):
            raise RemediationKubeError("target_not_registered")
        try:
            registered = self._registered(target)
        except Exception:
            raise RemediationKubeError("registration_unavailable") from None
        if registered is not True:
            raise RemediationKubeError("target_not_registered")

    def _actual(
        self, target: ActionTarget, raw: Document, *, from_list: bool = False
    ) -> ActionTarget:
        metadata = _dict(raw.get("metadata"))
        if (
            raw.get("kind", target.kind if from_list else None) != target.kind
            or raw.get("apiVersion", _RESOURCES[target.kind][0] if from_list else None)
            != _RESOURCES[target.kind][0]
            or metadata.get("uid") != target.uid
            or metadata.get("name") != target.name
            or metadata.get("namespace") != target.namespace
            or metadata.get("deletionTimestamp") is not None
        ):
            raise RemediationKubeError("drift")
        return target.model_copy(
            update={"resource_version": _string(metadata.get("resourceVersion"))},
        )

    def _read_target(self, target: ActionTarget) -> tuple[ActionTarget, Document]:
        raw = self._raw(target.kind, target.namespace, target.name)
        return self._actual(target, raw), raw

    def target(self, cluster: str, namespace: str, kind: str, name: str) -> ActionTarget:
        if not all(isinstance(value, str) and value for value in (cluster, namespace, kind, name)):
            raise RemediationKubeError("target_not_registered")
        target = ActionTarget(
            cluster=cluster,
            namespace=namespace,
            kind=kind,
            name=name,
            uid="",
            resource_version="",
        )
        self._register(target, _WORKLOADS | {"ConfigMap"}, resolving=True)
        raw = self._raw(kind, namespace, name)
        uid = _string(_dict(raw.get("metadata")).get("uid"))
        actual = self._actual(target.model_copy(update={"uid": uid}), raw)
        self._register(actual, _WORKLOADS | {"ConfigMap"})
        return actual

    def _exclusive_consumers(self, workload: ActionTarget, config_map: ActionTarget) -> None:
        try:
            exclusive = self._exclusive is not None and self._exclusive(workload, config_map)
        except Exception:
            raise RemediationKubeError("config_map_exclusivity_unknown") from None
        if exclusive is not True:
            raise RemediationKubeError("config_map_exclusivity_unknown")
        objects = {
            kind: self._items(kind, workload.namespace)
            for kind in sorted(_RESOURCES.keys() - {"ConfigMap"})
        }
        replica_sets = {
            _string(_dict(rs.get("metadata")).get("name")): rs for rs in objects["ReplicaSet"]
        }
        has_workload = False
        for kind, items in objects.items():
            for item in items:
                if not _config_map_reference(_pod_spec(kind, item), config_map.name):
                    continue
                if kind == workload.kind and _dict(item.get("metadata")).get("uid") == workload.uid:
                    observed = self._actual(workload, item, from_list=True)
                    if observed.resource_version != workload.resource_version:
                        raise RemediationKubeError("drift")
                    has_workload = True
                    continue
                if (
                    workload.kind == "StatefulSet"
                    and kind == "Pod"
                    and _owned_by(item, "StatefulSet", workload.name, workload.uid)
                ):
                    continue
                if workload.kind == "Deployment":
                    if kind == "ReplicaSet" and _owned_by(
                        item, "Deployment", workload.name, workload.uid
                    ):
                        continue
                    if kind == "Pod" and self._pod_owned_by_deployment(
                        item, workload, replica_sets
                    ):
                        continue
                raise RemediationKubeError("config_map_shared")
        if not has_workload:
            raise RemediationKubeError("config_map_consumer_unconfirmed")

    @staticmethod
    def _pod_owned_by_deployment(
        pod: Document, workload: ActionTarget, replica_sets: Mapping[str, Document]
    ) -> bool:
        owner = _owner(pod)
        name = owner.get("name")
        rs = replica_sets.get(name) if isinstance(name, str) else None
        if rs is None:
            return False
        metadata = _dict(rs.get("metadata"))
        return (
            metadata.get("name") == name
            and metadata.get("namespace") == workload.namespace
            and _owned_by(pod, "ReplicaSet", name, _string(metadata.get("uid")))
            and _owned_by(rs, "Deployment", workload.name, workload.uid)
        )

    def _snapshot(
        self,
        target: ActionTarget,
        kind: str,
        parameters: Mapping[str, Any],
        config_map_target: ActionTarget | None,
    ) -> tuple[Snapshot, Document]:
        args = _parameters(kind, parameters)
        self._register(target, _WORKLOADS)
        if args.get("source") == "config_map":
            if config_map_target is None or (
                config_map_target.cluster,
                config_map_target.namespace,
                config_map_target.name,
            ) != (target.cluster, target.namespace, args["config_map"]):
                raise RemediationKubeError("invalid_config_map_target")
            self._register(config_map_target, {"ConfigMap"})
        elif config_map_target is not None:
            raise RemediationKubeError("invalid_config_map_target")

        workload, raw = self._read_target(target)
        actual = workload
        if config_map_target is not None:
            actual, raw = self._read_target(config_map_target)
            if raw.get("immutable") is True:
                raise RemediationKubeError("config_value_unavailable")
            self._exclusive_consumers(workload, actual)
            live_workload, _ = self._read_target(workload)
            live_object, _ = self._read_target(actual)
            if live_workload != workload or live_object != actual:
                raise RemediationKubeError("drift")
        fields, paths = _fields(kind, args, raw)
        current = fields
        if kind == "adjust_resources":
            current, _ = _fields(
                kind, {"container": args["container"], **dict.fromkeys(_RESOURCE_FIELDS)}, raw
            )
        absent_env_value = args.get("source") == "env" and _path_value(raw, paths["value"]) is None
        return Snapshot(workload, actual, kind, args, fields, current, absent_env_value), raw

    @staticmethod
    def current_parameters(snapshot: Snapshot) -> dict[str, Scalar]:
        return dict(snapshot.current)

    def snapshot(
        self,
        target: ActionTarget,
        kind: str,
        parameters: Mapping[str, Any],
        *,
        config_map_target: ActionTarget | None = None,
    ) -> Snapshot:
        return self._snapshot(target, kind, parameters, config_map_target)[0]

    def _refresh(self, before: Snapshot) -> tuple[Snapshot, Document]:
        config_map = before.object if before.object.kind == "ConfigMap" else None
        return self._snapshot(before.workload, before.kind, before.parameters, config_map)

    def _plan(
        self,
        current: Snapshot,
        raw: Document,
        desired: Mapping[str, Scalar],
        *,
        forward: PatchPlan | None = None,
    ) -> PatchPlan:
        _, paths = _fields(current.kind, current.parameters, raw)
        changed = {
            key: value
            for key, value in desired.items()
            if not _same(key, current.fields[key], value)
        }
        if not changed:
            raise RemediationKubeError("no_change")
        self._check_rollout_strategy(current, raw)
        operations = _identity_tests(current.object)
        if current.object.kind != "ConfigMap" and current.kind != "scale_replicas":
            container = next(iter(paths.values()))[:5]
            operations.append(
                {
                    "op": "test",
                    "path": _pointer((*container, "name")),
                    "value": current.parameters["container"],
                }
            )
            if current.kind == "update_config":
                operations.append(
                    {
                        "op": "test",
                        "path": _pointer((*paths["value"][:-1], "name")),
                        "value": current.parameters["key"],
                    }
                )
        for path in paths.values():
            old = _path_value(raw, path)
            if old is not None:
                operations.append({"op": "test", "path": _pointer(path), "value": old})
        updated = deepcopy(raw)
        created_maps = []
        for key, value in changed.items():
            patch_value = None if forward is not None and forward.before.absent_env_value else value
            for parent in _patch_value(updated, paths[key], patch_value, operations):
                created_maps.append(tuple(str(part) for part in parent[5:]))
        if forward is not None and forward.created_resource_maps:
            container = next(iter(paths.values()))[:5]
            for suffix in sorted(forward.created_resource_maps, key=len, reverse=True):
                path = (*container, *suffix)
                if _path_value(updated, path) == {}:
                    _patch_value(updated, path, None, operations)
        return PatchPlan(
            before=current,
            after={**current.fields, **changed},
            body_json=json.dumps(operations, ensure_ascii=False, separators=(",", ":")),
            expected_spec_digest=_spec_digest(updated) if current.object.kind in _WORKLOADS else "",
            created_resource_maps=tuple(created_maps),
            restore=forward is not None,
        )

    @staticmethod
    def _check_rollout_strategy(current: Snapshot, raw: Document) -> None:
        if current.object.kind == "Deployment" and _dict(raw["spec"]).get("paused") is True:
            raise RemediationKubeError("rollout_unavailable")
        if current.object.kind == "StatefulSet" and current.kind != "scale_replicas":
            strategy = _optional(_dict(raw["spec"]), "updateStrategy")
            rolling = _optional(strategy, "rollingUpdate")
            if strategy.get("type", "RollingUpdate") != "RollingUpdate" or (
                rolling.get("partition", 0) != 0
            ):
                raise RemediationKubeError("rollout_unavailable")

    def prepare(self, snapshot: Snapshot) -> PatchPlan:
        current, raw = self._refresh(snapshot)
        if not _same_fields(snapshot.current, current.current):
            raise RemediationKubeError("drift")
        return self._plan(current, raw, {key: current.parameters[key] for key in current.fields})

    def prepare_restore(self, plan: PatchPlan) -> PatchPlan:
        if plan.restore:
            raise RemediationKubeError("invalid_restore")
        current, raw = self._refresh(plan.before)
        if not _same_fields(current.fields, plan.after):
            raise RemediationKubeError("drift")
        desired = {
            key: old
            for key, old in plan.before.fields.items()
            if not _same(key, old, plan.after[key])
        }
        return self._plan(current, raw, desired, forward=plan)

    def _revalidate(self, plan: PatchPlan) -> Document:
        current, raw = self._refresh(plan.before)
        if current.object != plan.before.object or not _same_fields(
            current.fields, plan.before.fields
        ):
            raise RemediationKubeError("drift")
        if current.object.kind == "ConfigMap" and current.workload != plan.before.workload:
            raise RemediationKubeError("drift")
        return raw

    def _response_receipt(
        self, plan: PatchPlan, previous: Document, updated: Document
    ) -> WriteReceipt:
        target = self._actual(plan.before.object, updated)
        fields, _ = _fields(plan.before.kind, plan.before.parameters, updated)
        if not _same_fields(fields, plan.after):
            raise RemediationKubeError("unexpected_write_fields")
        generation = None
        if target.kind == "ConfigMap":
            expected = _dict(previous.get("data")) | {
                _string(plan.before.parameters["key"]): plan.after["value"]
            }
            if (
                updated.get("data") != expected
                or updated.get("binaryData") != previous.get("binaryData")
                or updated.get("immutable") != previous.get("immutable")
            ):
                raise RemediationKubeError("unexpected_write_fields")
        else:
            if _spec_digest(updated) != plan.expected_spec_digest:
                raise RemediationKubeError("unexpected_write_spec")
            generation = _integer(_dict(updated.get("metadata")).get("generation"))
        return WriteReceipt(target, generation)

    def _patch(
        self, target: ActionTarget, body: list[Document], *, dry_run: bool = False
    ) -> Document:
        return self._request(
            "PATCH",
            target.kind,
            target.namespace,
            name=target.name,
            query=[("dryRun", "All")] if dry_run else None,
            body=body,
            real_write=not dry_run,
        )

    def dry_run(self, plan: PatchPlan) -> Preflight:
        # 预检凭证绑定账本数据的副本，发送时复核该副本未发生变化。
        plan = PatchPlan.from_dict(plan.to_dict())
        previous = self._revalidate(plan)
        updated = self._patch(plan.before.object, json.loads(plan.body_json), dry_run=True)
        self._response_receipt(plan, previous, updated)
        return Preflight(plan, (self._seal, _digest(plan.to_dict())))

    def send(self, preflight: Preflight, *, authorize: Callable[[], None]) -> WriteReceipt:
        plan = PatchPlan.from_dict(preflight.plan.to_dict())
        if preflight._seal != (self._seal, _digest(plan.to_dict())):
            raise RemediationKubeError("invalid_preflight")
        previous = self._revalidate(plan)
        body = json.loads(plan.body_json)
        authorize()
        result = self._patch(plan.before.object, body)
        try:
            receipt = self._response_receipt(plan, previous, result)
            if receipt.object.resource_version == plan.before.object.resource_version:
                raise RemediationKubeError("version_unchanged")
            if receipt.generation is not None and receipt.generation <= _integer(
                _dict(previous.get("metadata")).get("generation")
            ):
                raise RemediationKubeError("generation_unchanged")
            return receipt
        except RemediationKubeError:
            raise WriteUnknown("unconfirmed_write") from None

    def verify(self, plan: PatchPlan, receipt: WriteReceipt) -> Verification:
        try:
            current, raw = self._refresh(plan.before)
            if receipt.object.model_copy(
                update={"resource_version": current.object.resource_version}
            ) != current.object or not _same_fields(current.fields, plan.after):
                return Verification("drift")
            if current.object.kind == "ConfigMap":
                return Verification("ready")
            generation = _integer(_dict(raw.get("metadata")).get("generation"))
            if generation != receipt.generation or _spec_digest(raw) != plan.expected_spec_digest:
                return Verification("drift")
            status = _optional(raw, "status")
            replicas = _integer(_dict(raw.get("spec")).get("replicas"))
            if status.get("observedGeneration") != generation:
                return Verification("pending")
            ready = all(
                status.get(key, 0) == replicas
                for key in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")
            )
            if current.object.kind == "Deployment":
                ready = (
                    ready
                    and status.get("unavailableReplicas", 0) == 0
                    and status.get("terminatingReplicas", 0) == 0
                )
            else:
                update_revision = status.get("updateRevision")
                ready = (
                    ready
                    and bool(update_revision)
                    and status.get("currentRevision") == update_revision
                    and status.get("currentReplicas", 0) == replicas
                )
            return Verification("ready" if ready else "pending")
        except RemediationKubeError as exc:
            if (
                exc.code
                in {
                    "drift",
                    "config_map_shared",
                    "config_map_consumer_unconfirmed",
                    "config_value_unavailable",
                    "container_unavailable",
                }
                or exc.status == 404
            ):
                return Verification("drift")
            raise

    def inspect(self, plan: PatchPlan) -> Inspection:
        target = plan.before.object
        self._register(target, _WORKLOADS | {"ConfigMap"})
        actual, raw = self._read_target(target)
        fields, _ = _fields(plan.before.kind, plan.before.parameters, raw)
        if _same_fields(fields, plan.after):
            return Inspection("after", actual, fields)
        if _same_fields(fields, plan.before.fields):
            return Inspection("before", actual, fields)
        return Inspection("drift", actual, fields)

    def _isolation_target(self, target: ActionTarget, token: str) -> tuple[ActionTarget, Document]:
        self._register(target, _WORKLOADS | {"ConfigMap"})
        if not target.resource_version or not isinstance(token, str) or not 0 < len(token) <= 128:
            raise RemediationKubeError("invalid_isolation")
        return self._read_target(target)

    @staticmethod
    def _check_isolation(
        current: ActionTarget, raw: Document, token: str, excluded_versions: set[str]
    ) -> str:
        if current.resource_version in excluded_versions or (
            _optional(_dict(raw.get("metadata")), "annotations").get(RECONCILIATION_ANNOTATION)
            != token
        ):
            raise RemediationKubeError("isolation_unconfirmed")
        return current.resource_version

    def confirm_isolation(self, target: ActionTarget, token: str) -> str:
        current, raw = self._isolation_target(target, token)
        return self._check_isolation(current, raw, token, {target.resource_version})

    def isolate(self, target: ActionTarget, token: str) -> str:
        current, raw = self._isolation_target(target, token)
        metadata = _dict(raw.get("metadata"))
        annotations = _optional(metadata, "annotations")
        if annotations.get(RECONCILIATION_ANNOTATION) == token:
            return self._check_isolation(current, raw, token, {target.resource_version})
        body = _identity_tests(current)
        if "annotations" in metadata:
            body.append(
                {
                    "op": "add",
                    "path": _pointer(("metadata", "annotations", RECONCILIATION_ANNOTATION)),
                    "value": token,
                }
            )
        else:
            body.append(
                {
                    "op": "add",
                    "path": "/metadata/annotations",
                    "value": {RECONCILIATION_ANNOTATION: token},
                }
            )
        result = self._patch(current, body)
        try:
            updated = self._actual(current, result)
            return self._check_isolation(
                updated, result, token, {current.resource_version, target.resource_version}
            )
        except RemediationKubeError:
            raise WriteUnknown("isolation_unconfirmed") from None

    def pod_evidence(
        self,
        target: ActionTarget,
        container: str,
        *,
        now: datetime,
        max_age_seconds: float,
    ) -> tuple[PodEvidence, ...]:
        self._register(target, _WORKLOADS)
        if (
            not isinstance(container, str)
            or not container
            or not isinstance(now, datetime)
            or now.utcoffset() is None
            or type(max_age_seconds) not in (int, float)
            or not 0 < max_age_seconds < float("inf")
        ):
            raise RemediationKubeError("invalid_evidence_query")
        workload, _, pods = self._current_pods(target, container)
        results = [
            evidence
            for pod, rs_uid in pods
            if (evidence := self._oom_event(pod, workload, rs_uid, container, now, max_age_seconds))
            is not None
        ]
        return tuple(
            sorted(results, key=lambda item: (item.finished_at, item.pod_uid), reverse=True)
        )

    def current_pods(self, target: ActionTarget, container: str) -> tuple[str, ...]:
        self._register(target, _WORKLOADS)
        if not isinstance(container, str) or not container:
            raise RemediationKubeError("invalid_evidence_query")
        _, _, pods = self._current_pods(target, container)
        return tuple(sorted(_string(_dict(pod.get("metadata")).get("name")) for pod, _ in pods))

    def running_pod_containers(
        self, target: ActionTarget, container: str
    ) -> tuple[tuple[str, str], ...]:
        self._register(target, _WORKLOADS)
        if not isinstance(container, str) or not container:
            raise RemediationKubeError("invalid_evidence_query")
        _, _, pods = self._current_pods(target, container)
        instances = []
        for pod, _ in pods:
            statuses = _list(_optional(pod, "status").get("containerStatuses", []))
            matches = [status for status in statuses if _dict(status).get("name") == container]
            if len(matches) > 1:
                return ()
            if not matches or not isinstance(_optional(matches[0], "state").get("running"), dict):
                continue
            container_id = matches[0].get("containerID")
            if not isinstance(container_id, str) or not container_id:
                return ()
            name = _string(_dict(pod.get("metadata")).get("name"))
            instances.append((name, container_id))
        return tuple(sorted(instances))

    def observation_pods(
        self, plan: PatchPlan, receipt: WriteReceipt, container: str
    ) -> tuple[ObservedPod, ...]:
        target = plan.before.workload
        self._register(target, _WORKLOADS)
        _, raw, pods = self._current_pods(target, container)
        if (
            _dict(raw.get("metadata")).get("generation") != receipt.generation
            or _spec_digest(raw) != plan.expected_spec_digest
            or not 0 < len(pods) == _integer(_dict(raw.get("spec")).get("replicas"))
        ):
            raise RemediationKubeError("observation_pods_unconfirmed")
        results = []
        for pod, _ in pods:
            metadata = _dict(pod.get("metadata"))
            statuses = _list(_optional(pod, "status").get("containerStatuses", []))
            matches = [item for item in statuses if _dict(item).get("name") == container]
            if len(matches) != 1:
                raise RemediationKubeError("observation_container_unconfirmed")
            status = matches[0]
            oom_times = []
            for key in ("state", "lastState"):
                stopped = _optional(_optional(status, key), "terminated")
                if stopped.get("reason") == "OOMKilled":
                    try:
                        finished = datetime.fromisoformat(stopped["finishedAt"])
                        if finished.utcoffset() is None:
                            raise ValueError("missing timezone")
                    except (KeyError, TypeError, ValueError):
                        raise RemediationKubeError("oom_timestamp_unconfirmed") from None
                    oom_times.append(finished)
            results.append(
                ObservedPod(
                    name=_string(metadata.get("name")),
                    uid=_string(metadata.get("uid")),
                    container_id=_string(status.get("containerID")),
                    restarts=_integer(status.get("restartCount")),
                    oom_finished_at=tuple(sorted(set(oom_times))),
                )
            )
        return tuple(sorted(results, key=lambda pod: (pod.name, pod.uid)))

    def _current_pods(
        self, target: ActionTarget, container: str
    ) -> tuple[ActionTarget, Document, list[tuple[Document, str | None]]]:
        workload, raw = self._read_target(target)
        template = _template(raw)
        pod_spec = _dict(template.get("spec"))
        expected = deepcopy(_container(pod_spec, container)[1])
        _normalize_pod_spec({"containers": [expected]})
        labels = _dict(_dict(template.get("metadata")).get("labels"))
        if not labels or any(
            not _LABEL_KEY.fullmatch(key)
            or not isinstance(value, str)
            or not _LABEL_VALUE.fullmatch(value)
            for key, value in labels.items()
        ):
            raise RemediationKubeError("invalid_pod_selector")
        selector = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
        generation = _integer(_dict(raw.get("metadata")).get("generation"))
        status = _optional(raw, "status")
        if status.get("observedGeneration") != generation:
            raise RemediationKubeError("template_unconfirmed")
        revision = status.get("updateRevision")
        if target.kind == "StatefulSet" and not revision:
            raise RemediationKubeError("template_unconfirmed")
        expected_template = _rs_template_digest(raw) if target.kind == "Deployment" else ""
        rs_cache: dict[str, Document] = {}
        results = []
        for pod in self._items("Pod", workload.namespace, selector):
            metadata = _dict(pod.get("metadata"))
            if metadata.get("deletionTimestamp") is not None:
                continue
            rs_uid = None
            if target.kind == "Deployment":
                replica_set = self._current_replica_set(pod, workload, expected_template, rs_cache)
                if replica_set is None:
                    continue
                rs_uid = _string(_dict(replica_set.get("metadata")).get("uid"))
            elif not _owned_by(pod, "StatefulSet", workload.name, workload.uid) or (
                _optional(metadata, "labels").get("controller-revision-hash") != revision
            ):
                continue
            actual = deepcopy(_container(_pod_spec("Pod", pod), container)[1])
            _normalize_pod_spec({"containers": [actual]})
            if actual.get("image") != expected.get("image") or (
                actual.get("resources") != expected.get("resources")
            ):
                continue
            _string(metadata.get("uid"))
            results.append((pod, rs_uid))
        latest, _ = self._read_target(workload)
        if latest.resource_version != workload.resource_version:
            raise RemediationKubeError("drift")
        return workload, raw, results

    def _current_replica_set(
        self,
        pod: Document,
        workload: ActionTarget,
        template_digest: str,
        replica_sets: dict[str, Document],
    ) -> Document | None:
        owner = _owner(pod)
        if owner.get("kind") != "ReplicaSet" or owner.get("apiVersion") != "apps/v1":
            return None
        name = _string(owner.get("name"))
        if name not in replica_sets:
            try:
                replica_sets[name] = self._raw("ReplicaSet", workload.namespace, name)
            except RemediationKubeError as exc:
                if exc.status != 404:
                    raise
                return None
        rs = replica_sets[name]
        if not self._pod_owned_by_deployment(pod, workload, replica_sets):
            return None
        rs_labels = _optional(_dict(_template(rs).get("metadata")), "labels")
        pod_labels = _optional(_dict(pod.get("metadata")), "labels")
        template_hash = rs_labels.get("pod-template-hash")
        if (
            rs.get("kind") != "ReplicaSet"
            or rs.get("apiVersion") != "apps/v1"
            or _dict(rs.get("metadata")).get("deletionTimestamp") is not None
            or not template_hash
            or pod_labels.get("pod-template-hash") != template_hash
            or _rs_template_digest(rs) != template_digest
        ):
            return None
        return rs

    @staticmethod
    def _oom_event(
        pod: Document,
        workload: ActionTarget,
        replica_set_uid: str | None,
        container: str,
        now: datetime,
        max_age_seconds: float,
    ) -> PodEvidence | None:
        metadata = _dict(pod.get("metadata"))
        statuses = _list(_optional(pod, "status").get("containerStatuses", []))
        matching = [item for item in statuses if _dict(item).get("name") == container]
        if len(matching) != 1:
            return None
        status = matching[0]
        terminated = _optional(status, "state").get("terminated")
        if terminated is None:
            terminated = _optional(status, "lastState").get("terminated")
        if not isinstance(terminated, dict) or terminated.get("reason") != "OOMKilled":
            return None
        try:
            finished = datetime.fromisoformat(terminated["finishedAt"])
        except (KeyError, TypeError, ValueError):
            return None
        if finished.utcoffset() is None or not (
            0 <= (now - finished).total_seconds() <= max_age_seconds
        ):
            return None
        container_id = terminated.get("containerID")
        return PodEvidence(
            workload_uid=workload.uid,
            replica_set_uid=replica_set_uid,
            pod_name=_string(metadata.get("name")),
            pod_uid=_string(metadata.get("uid")),
            container=container,
            finished_at=finished,
            restart_count=_integer(status.get("restartCount")),
            container_id=container_id if isinstance(container_id, str) else None,
        )
