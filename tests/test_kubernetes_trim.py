"""Kubernetes 适配器只暴露裁剪后的只读字段，不把完整对象交给模型。"""

from aiops.tools.kubernetes import trim_deployment, trim_event, trim_pod


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
