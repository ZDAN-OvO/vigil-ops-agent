"""从启动时使用的同一份策略生成专用身份 RBAC 和字段准入。"""

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

from aiops.actions.remediation_policy import (
    RECONCILIATION_ANNOTATION,
    REMEDIATION_IDENTITY,
    RemediationPolicy,
    load_policy,
)

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "vigil-remediation"
ACCOUNT_NAMESPACE = "vigil-monitoring"
POLICY_NAME = "vigil-remediation-write"
CONSUMER_POLICY_NAME = "vigil-remediation-config-consumers"
WORKLOADS = {"Deployment": "deployments", "StatefulSet": "statefulsets"}


class Block(str):
    pass


class ManifestDumper(yaml.SafeDumper):
    pass


ManifestDumper.add_representer(
    Block, lambda dumper, value: dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")
)


def _literal(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _cel_map(value: dict) -> str:
    # CEL 的同一字典需要一致的值类型。登记项同时包含列表、字典和整数。
    pairs = []
    for key, item in sorted(value.items()):
        expression = _cel_map(item) if isinstance(item, dict) else _literal(item)
        pairs.append(f"{_literal(key)}:dyn({expression})")
    return "{" + ",".join(pairs) + "}"


def _unchanged(new: str, old: str, except_keys: str) -> str:
    """双向比较键和内容，涵盖未预先列出的字段以及添加与删除。"""
    return (
        f"({new}).all(k, k in {except_keys} || "
        f"(k in ({old}) && ({new})[k] == ({old})[k])) &&\n"
        f"({old}).all(k, k in {except_keys} || k in ({new}))"
    )


def _optional(value: str, field: str, fallback: str = "{}") -> str:
    return f"({field!r} in {value} ? {value}.{field} : {fallback})"


def _role_rule(
    group: str, resources: list[str], verbs: list[str], names: list[str] | None = None
) -> dict:
    rule = {"apiGroups": [group], "resources": resources, "verbs": verbs}
    if names is not None:
        rule["resourceNames"] = names
    return rule


def render_rbac(policy: RemediationPolicy) -> str:
    names_by_namespace = defaultdict(lambda: defaultdict(set))
    for service in policy.services:
        names_by_namespace[service.namespace][WORKLOADS[service.kind]].add(service.name)
        for field in service.config_fields:
            if field.config_map is not None:
                names_by_namespace[service.namespace]["configmaps"].add(field.config_map)

    documents = [
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": ACCOUNT, "namespace": ACCOUNT_NAMESPACE},
            "automountServiceAccountToken": False,
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": ACCOUNT},
            "rules": [
                _role_rule(
                    "admissionregistration.k8s.io",
                    ["validatingadmissionpolicies", "validatingadmissionpolicybindings"],
                    ["get"],
                    [POLICY_NAME, CONSUMER_POLICY_NAME],
                ),
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": {"name": ACCOUNT},
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "ClusterRole",
                "name": ACCOUNT,
            },
            "subjects": [
                {"kind": "ServiceAccount", "name": ACCOUNT, "namespace": ACCOUNT_NAMESPACE}
            ],
        },
    ]
    for namespace, names in sorted(names_by_namespace.items()):
        rules = [
            _role_rule("", ["pods"], ["get", "list", "patch"]),
            _role_rule("", ["pods/log"], ["get"]),
            _role_rule("", ["configmaps", "services", "events", "endpoints"], ["get", "list"]),
            _role_rule("", ["podtemplates", "replicationcontrollers"], ["list"]),
            _role_rule(
                "apps",
                ["deployments", "statefulsets", "replicasets", "daemonsets"],
                ["get", "list"],
            ),
            _role_rule("batch", ["jobs", "cronjobs"], ["get", "list"]),
            _role_rule("events.k8s.io", ["events"], ["get", "list"]),
            _role_rule("discovery.k8s.io", ["endpointslices"], ["get", "list"]),
            _role_rule("metrics.k8s.io", ["pods"], ["get", "list"]),
        ]
        for resource, resource_names in sorted(names.items()):
            group = "" if resource == "configmaps" else "apps"
            rules.append(_role_rule(group, [resource], ["patch"], sorted(resource_names)))
        documents.extend(
            [
                {
                    "apiVersion": "rbac.authorization.k8s.io/v1",
                    "kind": "Role",
                    "metadata": {"name": ACCOUNT, "namespace": namespace},
                    "rules": rules,
                },
                {
                    "apiVersion": "rbac.authorization.k8s.io/v1",
                    "kind": "RoleBinding",
                    "metadata": {"name": ACCOUNT, "namespace": namespace},
                    "roleRef": {
                        "apiGroup": "rbac.authorization.k8s.io",
                        "kind": "Role",
                        "name": ACCOUNT,
                    },
                    "subjects": [
                        {"kind": "ServiceAccount", "name": ACCOUNT, "namespace": ACCOUNT_NAMESPACE}
                    ],
                },
            ]
        )
    return _render(documents)


def _parameters(policy: RemediationPolicy) -> dict:
    targets = {}
    for service in policy.services:
        definition: dict[str, Any] = {"replicas": 0, "resources": {}, "env": {}}
        if service.replicas is not None:
            definition["replicas"] = service.replicas.max
        for container, fields in service.resources.items():
            target = definition["resources"][container] = {"requests": {}, "limits": {}}
            for field, bounds in fields.items():
                group, resource = field.split("_", 1)
                target[f"{group}s"][resource] = bounds.hard_max
        for field in service.config_fields:
            if field.source == "env":
                definition["env"].setdefault(field.container, {})[field.key] = field.allowed_values
            else:
                key = f"{service.namespace}/configmaps/{field.config_map}"
                targets.setdefault(key, {})[field.key] = field.allowed_values
        targets[f"{service.namespace}/{WORKLOADS[service.kind]}/{service.name}"] = definition
    return targets


def _resource_map(new: str, old: str, allowed: str) -> str:
    return f"""(
{_unchanged(new, old, allowed)} &&
({new}).all(k, k in ({old}) && (({new})[k] == ({old})[k] ||
  (k in {allowed} && isQuantity(string(({new})[k])) &&
   quantity(string(({new})[k])).isGreaterThan(quantity('0')) &&
   !quantity(string(({new})[k])).isGreaterThan(quantity({allowed}[k])))))
)"""


def _container_resources() -> str:
    new, old = "n.resources", "o.resources"
    allowed = "variables.target.resources[n.name]"

    def resource_map(group: str) -> str:
        return _resource_map(_optional(new, group), _optional(old, group), f"{allowed}.{group}")

    def resource_pair(key: str) -> str:
        requests = _optional(new, "requests")
        limits = _optional(new, "limits")
        return f"""(
{key!r} in {requests} && {key!r} in {limits}
 ? !quantity(string({requests}[{key!r}])).isGreaterThan(quantity(string({limits}[{key!r}])))
 : true
)"""

    return f"""(
n.?resources == o.?resources ||
(n.name in variables.target.resources && has(n.resources) && has(o.resources) &&
{_unchanged(new, old, "['requests', 'limits']")} &&
n.resources.?requests.hasValue() == o.resources.?requests.hasValue() &&
n.resources.?limits.hasValue() == o.resources.?limits.hasValue() &&
{resource_map("requests")} &&
{resource_map("limits")} &&
{resource_pair("cpu")} &&
{resource_pair("memory")})
)"""


def _container_env() -> str:
    allowed = "variables.target.env[n.name][e.name]"
    return f"""(
n.?env == o.?env ||
(n.name in variables.target.env && has(n.env) && has(o.env) &&
n.env.map(e, e.name) == o.env.map(e, e.name) &&
n.env.all(e, o.env.all(p, e.name != p.name || e == p || (
  e.name in variables.target.env[n.name] &&
  {_unchanged("e", "p", "['value']")} &&
  !has(e.valueFrom) && !has(p.valueFrom) &&
  has(e.value) && has(p.value) && e.value in {allowed} && p.value in {allowed}
))))
)"""


def _workload_checks() -> str:
    new_pod_spec = "variables.new.spec.template.spec"
    old_pod_spec = "variables.old.spec.template.spec"
    return f"""
{_unchanged("variables.new.spec", "variables.old.spec", "['replicas', 'template']")} &&
(variables.new.spec.?replicas == variables.old.spec.?replicas ||
  (variables.target.replicas > 0 && has(variables.new.spec.replicas) &&
   has(variables.old.spec.replicas) &&
   variables.new.spec.replicas >= 1 && variables.new.spec.replicas <= variables.target.replicas)) &&
{_unchanged("variables.new.spec.template", "variables.old.spec.template", "['spec']")} &&
{_unchanged(new_pod_spec, old_pod_spec, "['containers']")} &&
variables.containers.map(n, n.name) == variables.oldContainers.map(o, o.name) &&
variables.containers.all(n, variables.oldContainers.all(o, n.name != o.name || n == o || (
  {_unchanged("n", "o", "['resources', 'env']")} &&
  {_container_resources()} &&
  {_container_env()}
)))
""".strip()


def _config_map_checks() -> str:
    new, old = "variables.newData", "variables.oldData"
    return f"""
{new}.size() == {old}.size() &&
{new}.all(k, k in {old} && ({new}[k] == {old}[k] ||
  (k in variables.target && {new}[k] in variables.target[k] && {old}[k] in variables.target[k])))
""".strip()


def _config_consumers(policy: RemediationPolicy) -> list[dict]:
    """把登记的独占 ConfigMap 绑定到唯一工作负载及其控制器创建的子对象。"""
    reservations = {
        (service.namespace, field.config_map): {
            "namespace": service.namespace,
            "configMap": field.config_map,
            "resource": WORKLOADS[service.kind],
            "workload": service.name,
        }
        for service in policy.services
        for field in service.config_fields
        if field.config_map is not None
    }
    if not reservations:
        return []
    name = CONSUMER_POLICY_NAME
    variables = {
        "reservations": _literal([reservations[key] for key in sorted(reservations)]),
        "resource": "request.resource.resource",
        "spec": "dyn(object).?spec.orValue({})",
        "podSpec": (
            "variables.resource == 'pods' ? variables.spec : "
            "variables.resource == 'podtemplates' ? dyn(object).template.spec : "
            "variables.resource == 'cronjobs' ? variables.spec.jobTemplate.spec.template.spec : "
            "variables.spec.?template.orValue({}).?spec.orValue({})"
        ),
        "owners": "dyn(object).metadata.?ownerReferences.orValue([])",
        "containers": (
            "variables.podSpec.?containers.orValue([]) + "
            "variables.podSpec.?initContainers.orValue([]) + "
            "variables.podSpec.?ephemeralContainers.orValue([])"
        ),
        "volumes": "variables.podSpec.?volumes.orValue([])",
        "username": "request.userInfo.username",
    }
    references = """(
variables.volumes.exists(v,
  v.?configMap.orValue({}).?name.orValue('') == r.configMap ||
  v.?projected.orValue({}).?sources.orValue([]).exists(s,
    s.?configMap.orValue({}).?name.orValue('') == r.configMap
  )
) ||
variables.containers.exists(c,
  c.?envFrom.orValue([]).exists(e, e.?configMapRef.orValue({}).?name.orValue('') == r.configMap) ||
  c.?env.orValue([]).exists(e,
    e.?valueFrom.orValue({}).?configMapKeyRef.orValue({}).?name.orValue('') == r.configMap
  )
)
)"""
    permitted = """(
(variables.resource == r.resource && request.name == r.workload) ||
(variables.resource == 'replicasets' && r.resource == 'deployments' &&
 variables.username in [
   'system:kube-controller-manager', 'system:serviceaccount:kube-system:deployment-controller'
 ] &&
 variables.owners.exists(o,
   o.?controller.orValue(false) && o.apiVersion == 'apps/v1' && o.kind == 'Deployment' &&
   o.name == r.workload && o.uid != ''
 )) ||
(variables.resource == 'pods' &&
 variables.owners.exists(o,
   o.?controller.orValue(false) && o.apiVersion == 'apps/v1' && o.uid != '' && (
     (r.resource == 'deployments' && o.kind == 'ReplicaSet' &&
      o.name.startsWith(r.workload + '-') && variables.username in [
        'system:kube-controller-manager', 'system:serviceaccount:kube-system:replicaset-controller'
      ]) ||
     (r.resource == 'statefulsets' && o.kind == 'StatefulSet' && o.name == r.workload &&
      variables.username in [
        'system:kube-controller-manager', 'system:serviceaccount:kube-system:statefulset-controller'
      ])
   )
 ))
)"""
    expression = (
        f"variables.reservations.all(r, request.namespace != r.namespace || "
        f"!{references} || {permitted})"
    )
    rules = {
        "": ["pods", "pods/ephemeralcontainers", "podtemplates", "replicationcontrollers"],
        "apps": ["deployments", "statefulsets", "replicasets", "daemonsets"],
        "batch": ["cronjobs", "jobs"],
    }
    api = "admissionregistration.k8s.io/v1"
    return [
        {
            "apiVersion": api,
            "kind": "ValidatingAdmissionPolicy",
            "metadata": {
                "name": name,
                "annotations": {"vigil.aiops/policy-version": policy.version},
            },
            "spec": {
                "failurePolicy": "Fail",
                "matchConstraints": {
                    "resourceRules": [
                        {
                            "apiGroups": [group],
                            "apiVersions": ["v1"],
                            "operations": ["CREATE", "UPDATE"],
                            "resources": resources,
                            "scope": "Namespaced",
                        }
                        for group, resources in rules.items()
                    ],
                },
                "variables": [
                    {"name": variable, "expression": Block(value)}
                    for variable, value in variables.items()
                ],
                "validations": [
                    {
                        "expression": Block(expression),
                        "message": "ConfigMap reserved for one remediation workload",
                    }
                ],
            },
        },
        {
            "apiVersion": api,
            "kind": "ValidatingAdmissionPolicyBinding",
            "metadata": {"name": name},
            "spec": {"policyName": name, "validationActions": ["Deny"]},
        },
    ]


def render_admission(policy: RemediationPolicy) -> str:
    annotation = _literal(RECONCILIATION_ANNOTATION)
    exceptions = "['annotations', 'generation', 'managedFields']"
    metadata = f"""
{_unchanged("variables.new.metadata", "variables.old.metadata", exceptions)} &&
{_unchanged("variables.annotations", "variables.oldAnnotations", f"[{annotation}]")} &&
(!variables.fenceChanged || (
  {annotation} in variables.annotations &&
  variables.annotations[{annotation}].matches('^[A-Za-z0-9._:-]{{1,128}}$')
))
""".strip()
    # 动态字典保留所有 API 对象字段，连同将来版本新增的字段一起比较。
    variables = {
        "new": "dyn(object)",
        "old": "dyn(oldObject)",
        "targets": _cel_map(_parameters(policy)),
        "key": "request.namespace + '/' + request.resource.resource + '/' + request.name",
        "target": "variables.targets[variables.key]",
        "isConfig": "request.resource.resource == 'configmaps'",
        "annotations": _optional("variables.new.metadata", "annotations"),
        "oldAnnotations": _optional("variables.old.metadata", "annotations"),
        "fenceChanged": (
            f"variables.annotations[?{annotation}] != variables.oldAnnotations[?{annotation}]"
        ),
        "containers": "variables.new.spec.template.spec.containers",
        "oldContainers": "variables.old.spec.template.spec.containers",
        "newData": _optional("variables.new", "data"),
        "oldData": _optional("variables.old", "data"),
    }
    # 第一条表达式也约束读权限不小心被扩张后的 Pod 和子资源写入。
    registered_update = """
request.operation == 'UPDATE' &&
(!has(request.subResource) || request.subResource == '') &&
variables.key in variables.targets && oldObject != null && object != null
""".strip()
    config_fields = "['metadata', 'data']"
    workload_fields = "['metadata', 'spec']"
    config_checks = (
        f"{_unchanged('variables.new', 'variables.old', config_fields)} &&\n"
        f"(variables.fenceChanged ? variables.new.?data == variables.old.?data : "
        f"({_config_map_checks()}))"
    )
    workload_checks = (
        f"{_unchanged('variables.new', 'variables.old', workload_fields)} &&\n"
        "(variables.fenceChanged ? variables.new.spec == variables.old.spec : "
        f"({_workload_checks()}))"
    )
    identity = _literal(REMEDIATION_IDENTITY)
    return _render(
        [
            {
                "apiVersion": "admissionregistration.k8s.io/v1",
                "kind": "ValidatingAdmissionPolicy",
                "metadata": {
                    "name": POLICY_NAME,
                    "annotations": {"vigil.aiops/policy-version": policy.version},
                },
                "spec": {
                    "failurePolicy": "Fail",
                    "matchConstraints": {
                        "resourceRules": [
                            {
                                "apiGroups": ["*"],
                                "apiVersions": ["*"],
                                "operations": ["CREATE", "UPDATE", "DELETE", "CONNECT"],
                                "resources": ["*/*"],
                                "scope": "Namespaced",
                            }
                        ],
                    },
                    "matchConditions": [
                        {
                            "name": "dedicated-remediation-identity",
                            "expression": f"request.userInfo.username == {identity}",
                        }
                    ],
                    "variables": [
                        {"name": name, "expression": Block(expression)}
                        for name, expression in variables.items()
                    ],
                    "validations": [
                        {
                            "expression": Block(
                                f"({registered_update}) &&\n({metadata}) &&\n"
                                f"(variables.isConfig ? ({config_checks}) : ({workload_checks}))"
                            ),
                            "message": "Vigil remediation write exceeds the registered hard bounds",
                        }
                    ],
                },
            },
            {
                "apiVersion": "admissionregistration.k8s.io/v1",
                "kind": "ValidatingAdmissionPolicyBinding",
                "metadata": {"name": POLICY_NAME},
                "spec": {"policyName": POLICY_NAME, "validationActions": ["Deny"]},
            },
            *_config_consumers(policy),
        ]
    )


def _render(documents: list[dict]) -> str:
    return "# 由 scripts/generate_remediation_manifests.py 生成。\n" + yaml.dump_all(
        documents, Dumper=ManifestDumper, allow_unicode=True, sort_keys=False, width=100
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, default=ROOT / "config/remediation")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--check", action="store_true")
    options = parser.parse_args()
    default_config = (ROOT / "config/remediation").resolve()
    default_output = (ROOT / "env/manifests").resolve()
    output = (options.output_dir or default_output).resolve()
    if options.config_dir.resolve() != default_config and output == default_output:
        parser.error("非默认策略需要单独的 --output-dir")
    policy = load_policy(options.config_dir)
    if policy.error is not None:
        parser.error(policy.error)
    files = {
        "remediation-rbac.yaml": render_rbac(policy),
        "remediation-admission.yaml": render_admission(policy),
    }
    if options.check:
        for filename, expected in files.items():
            file = output / filename
            if not file.is_file() or file.read_bytes() != expected.encode():
                parser.error(f"{filename} 与当前策略不一致")
        return
    output.mkdir(parents=True, exist_ok=True)
    for filename, content in files.items():
        (output / filename).write_bytes(content.encode())


if __name__ == "__main__":
    main()
