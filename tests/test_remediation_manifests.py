"""策略装载和部署产物使用同一份范围与硬边界。"""

from pathlib import Path
from runpy import run_path

import yaml

from aiops.actions.remediation_kube import _RESOURCES
from aiops.actions.remediation_policy import REMEDIATION_IDENTITY, load_policy

ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = ROOT / "env/manifests"
CONFIG = ROOT / "config/remediation"


def test_remediation_manifests_reproduce_byte_for_byte():
    generator = run_path(str(ROOT / "scripts/generate_remediation_manifests.py"))
    policy = load_policy(CONFIG)
    assert policy.error is None

    assert (MANIFESTS / "remediation-rbac.yaml").read_bytes() == generator["render_rbac"](
        policy
    ).encode()
    admission = generator["render_admission"](policy)
    assert (MANIFESTS / "remediation-admission.yaml").read_bytes() == admission.encode()

    isolated_policy = load_policy(CONFIG / "isolated-acceptance")
    assert isolated_policy.version != policy.version
    assert generator["render_rbac"](isolated_policy) == generator["render_rbac"](policy)
    isolated = generator["render_admission"](isolated_policy)
    for document in yaml.safe_load_all(isolated):
        if document["kind"] == "ValidatingAdmissionPolicy":
            assert document["metadata"]["annotations"]["vigil.aiops/policy-version"] == (
                isolated_policy.version
            )
    assert "worsen" not in admission
    assert "worsen" in isolated


def test_rbac_has_only_namespaced_and_registered_writes():
    documents = list(
        yaml.safe_load_all((MANIFESTS / "remediation-rbac.yaml").read_text(encoding="utf-8"))
    )
    [cluster_role] = [doc for doc in documents if doc["kind"] == "ClusterRole"]
    assert cluster_role["rules"] == [
        {
            "apiGroups": ["admissionregistration.k8s.io"],
            "resources": ["validatingadmissionpolicies", "validatingadmissionpolicybindings"],
            "verbs": ["get"],
            "resourceNames": ["vigil-remediation-write", "vigil-remediation-config-consumers"],
        }
    ]
    [cluster_binding] = [doc for doc in documents if doc["kind"] == "ClusterRoleBinding"]
    assert cluster_binding["roleRef"]["name"] == cluster_role["metadata"]["name"]
    assert {doc["metadata"]["namespace"] for doc in documents if doc["kind"] == "Role"} == {
        "vigil-shop",
        "vigil-experiment",
    }
    patchable = {}
    for role in (doc for doc in documents if doc["kind"] == "Role"):
        readable = {
            resource
            for rule in role["rules"]
            if "list" in rule["verbs"]
            for resource in rule["resources"]
        }
        assert {resource for _, resource in _RESOURCES.values()} <= readable
        for rule in role["rules"]:
            assert set(rule["verbs"]) <= {"get", "list", "patch"}
            assert not {"secrets", "persistentvolumeclaims", "serviceaccounts", "roles"} & set(
                rule["resources"]
            )
            if "patch" in rule["verbs"]:
                patchable[(role["metadata"]["namespace"], *rule["resources"])] = rule.get(
                    "resourceNames", []
                )
    assert patchable == {
        ("vigil-shop", "pods"): [],
        ("vigil-shop", "deployments"): ["currencyservice"],
        ("vigil-experiment", "pods"): [],
        ("vigil-experiment", "deployments"): ["vigil-config-fixture", "vigil-replica-fixture"],
        ("vigil-experiment", "configmaps"): ["vigil-config-fixture"],
    }


def test_admission_enforces_deny_for_dedicated_identity():
    admission, binding, consumer_policy, consumer_binding = yaml.safe_load_all(
        (MANIFESTS / "remediation-admission.yaml").read_text(encoding="utf-8")
    )
    assert admission["spec"]["failurePolicy"] == "Fail"
    assert REMEDIATION_IDENTITY in admission["spec"]["matchConditions"][0]["expression"]
    assert binding["spec"]["validationActions"] == ["Deny"]
    assert binding["spec"]["policyName"] == admission["metadata"]["name"]
    rules = consumer_policy["spec"]["matchConstraints"]["resourceRules"]
    assert {"pods", "pods/ephemeralcontainers", "podtemplates", "replicationcontrollers"} <= set(
        rules[0]["resources"]
    )
    assert {"deployments", "replicasets", "statefulsets", "daemonsets"} <= set(
        rules[1]["resources"]
    )
    assert consumer_binding["spec"]["validationActions"] == ["Deny"]
