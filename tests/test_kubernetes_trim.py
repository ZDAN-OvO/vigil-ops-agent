"""Kubernetes 适配器只暴露裁剪后的只读字段，不把完整对象交给模型。"""

from aiops.tools.kubernetes import (
    matches_selector,
    matches_service_selector,
    trim_deployment,
    trim_event,
    trim_pod,
    trim_replica_set,
    trim_service,
)


def test_pod_trim_keeps_failure_state_without_spec_noise():
    pod = {
        "metadata": {"name": "frontend-abc", "uid": "uid-1", "labels": {"app": "frontend"}},
        "spec": {
            "node_name": "k3d-vigil-lab-server-0",
            "containers": [{"env": [{"name": "TOKEN"}]}],
        },
        "status": {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "True"}],
            "container_statuses": [
                {
                    "name": "server",
                    "image": "frontend:1",
                    "ready": True,
                    "restart_count": 3,
                    "state": {"terminated": {"reason": "OOMKilled", "exit_code": 137}},
                }
            ],
        },
    }

    trimmed = trim_pod(pod)

    assert trimmed["name"] == "frontend-abc"
    assert trimmed["containers"][0]["reason"] == "OOMKilled"
    assert trimmed["containers"][0]["exit_code"] == 137
    assert trimmed["conditions"] == {"Ready": "True"}
    assert "spec" not in trimmed
    assert "TOKEN" not in str(trimmed)


def test_deployment_trim_exposes_rollout_state():
    deployment = {
        "metadata": {"name": "productcatalogservice", "uid": "uid-2", "generation": 9},
        "spec": {
            "strategy": {"type": "RollingUpdate"},
            "selector": {"match_labels": {"app": "productcatalogservice"}},
            "template": {"spec": {"containers": [{"image": "productcatalogservice@sha256:bad"}]}},
        },
        "status": {
            "observed_generation": 9,
            "replicas": 1,
            "updated_replicas": 1,
            "available_replicas": 1,
            "unavailable_replicas": 0,
        },
    }

    trimmed = trim_deployment(deployment)

    assert trimmed["generation"] == 9
    assert trimmed["observed_generation"] == 9
    assert trimmed["available_replicas"] == 1
    assert trimmed["images"] == ["productcatalogservice@sha256:bad"]
    assert trimmed["selector"] == {"app": "productcatalogservice"}


def test_event_trim_points_at_the_involved_object():
    event = {
        "metadata": {"name": "frontend.1"},
        "type": "Warning",
        "reason": "Unhealthy",
        "message": "Readiness probe failed",
        "count": 4,
        "involved_object": {"kind": "Pod", "name": "frontend-abc"},
        "first_timestamp": "2026-11-04T09:00:00Z",
        "last_timestamp": "2026-11-04T09:03:00Z",
    }

    trimmed = trim_event(event)

    assert trimmed["involved"] == {"kind": "Pod", "name": "frontend-abc"}
    assert trimmed["reason"] == "Unhealthy"
    assert trimmed["count"] == 4


def test_pod_trim_keeps_last_termination_and_resources():
    pod = {
        "metadata": {"name": "currencyservice-abc", "uid": "uid-3", "labels": {}},
        "spec": {
            "containers": [
                {
                    "name": "server",
                    "env": [{"name": "TOKEN", "value": "secret"}],
                    "resources": {
                        "limits": {"memory": "16Mi"},
                        "requests": {"memory": "8Mi"},
                    },
                }
            ]
        },
        "status": {
            "phase": "Running",
            "container_statuses": [
                {
                    "name": "server",
                    "image": "currencyservice@sha256:x",
                    "ready": False,
                    "restart_count": 4,
                    "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                    "last_state": {
                        "terminated": {
                            "reason": "OOMKilled",
                            "exit_code": 137,
                            "finished_at": "2026-11-04T16:51:00Z",
                        }
                    },
                }
            ],
        },
    }

    container = trim_pod(pod)["containers"][0]

    assert container["reason"] == "CrashLoopBackOff"
    assert container["restart_count"] == 4
    assert container["last_terminated"] == {
        "reason": "OOMKilled",
        "exit_code": 137,
        "finished_at": "2026-11-04T16:51:00Z",
    }
    assert container["resources"] == {
        "limits": {"memory": "16Mi"},
        "requests": {"memory": "8Mi"},
    }
    assert "TOKEN" not in str(container)


def test_deployment_trim_exposes_resources_and_revision():
    deployment = {
        "metadata": {
            "name": "currencyservice",
            "uid": "uid-4",
            "generation": 4,
            "annotations": {
                "deployment.kubernetes.io/revision": "4",
                "kubernetes.io/change-cause": "experiment",
            },
        },
        "spec": {
            "selector": {"match_labels": {"app": "currencyservice"}},
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "server",
                            "image": "currencyservice@sha256:x",
                            "resources": {"limits": {"memory": "16Mi"}},
                        }
                    ]
                }
            },
        },
        "status": {"replicas": 1, "available_replicas": 0},
    }

    trimmed = trim_deployment(deployment)

    assert trimmed["revision"] == "4"
    assert trimmed["change_cause"] == "experiment"
    assert trimmed["containers"][0]["limits"] == {"memory": "16Mi"}


def test_replica_set_trim_exposes_template_resources():
    replicaset = {
        "metadata": {
            "annotations": {"deployment.kubernetes.io/revision": "3"},
            "creation_timestamp": "2026-11-04T16:50:00Z",
            "owner_references": [{"kind": "Deployment", "uid": "uid-4"}],
        },
        "spec": {
            "replicas": 0,
            "template": {
                "spec": {
                    "containers": [
                        {
                            "image": "currencyservice@sha256:x",
                            "resources": {"limits": {"memory": "128Mi"}},
                        }
                    ]
                }
            },
        },
        "status": {"ready_replicas": 0},
    }

    trimmed = trim_replica_set(replicaset)

    assert trimmed["revision"] == 3
    assert trimmed["memory_limit"] == "128Mi"
    assert trimmed["owners"] == [{"kind": "Deployment", "uid": "uid-4"}]


def test_deployment_selector_matches_template_labels():
    item = {"labels": {}, "selector": {"app": "currencyservice"}}

    assert matches_selector("app=currencyservice", item)
    assert not matches_selector("app=frontend", item)


def test_service_trim_exposes_selector_and_ports():
    service = {
        "metadata": {"name": "redis-cart", "labels": {}},
        "spec": {
            "type": "ClusterIP",
            "cluster_ip": "10.43.105.174",
            "selector": {"app": "redis-cart"},
            "ports": [{"name": "tcp-redis", "port": 6379, "target_port": 6379}],
        },
    }

    trimmed = trim_service(service)

    assert trimmed["name"] == "redis-cart"
    assert trimmed["selector"] == {"app": "redis-cart"}
    assert trimmed["ports"][0]["port"] == 6379


def test_service_selector_accepts_name_or_label():
    item = {"name": "redis-cart", "labels": {"app": "redis-cart"}, "selector": {}}

    assert matches_service_selector("redis-cart", item)
    assert matches_service_selector("app=redis-cart", item)
    assert not matches_service_selector("app=frontend", item)
