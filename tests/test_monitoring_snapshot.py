"""固定响应样例验证值班查询、缺数和归属边界。"""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from aiops.monitoring.releases import REVISION, REVISION_HISTORY, RollbackRecord
from aiops.monitoring.signals import NODE_QUERY
from aiops.monitoring.snapshot import Monitoring, grafana_links

NOW = datetime(2026, 11, 21, 10, tzinfo=UTC)
SLOT = int(NOW.timestamp())
APP = "online-boutique"
ADDRESS = "http://frontend.vigil-shop.svc.cluster.local./product/OLJCESPC7Z"


def _owner(kind, uid, controller=True):
    return {"kind": kind, "uid": uid, "controller": controller}


def _deployment(uid, name, *, desired=1, ready=1):
    return {
        "metadata": {"uid": uid, "name": name, "generation": 1},
        "spec": {"replicas": desired, "template": {"metadata": {"labels": {"app": name}}}},
        "status": {"ready_replicas": ready, "observed_generation": 1},
    }


def _replicaset(uid, deployment, revision, *, old="", cause="", created=None):
    return {
        "metadata": {
            "uid": uid,
            "creation_timestamp": created or NOW - timedelta(hours=2),
            "owner_references": [_owner("Deployment", deployment)],
            "annotations": {
                REVISION: str(revision),
                REVISION_HISTORY: old,
                "kubernetes.io/change-cause": cause,
            },
        },
        "spec": {"template": {"spec": {"containers": [{"image": f"shop:v{old or revision}"}]}}},
    }


def _pod(uid, rs_uid, *, name="catalog-1", count=0, state=None, created=None, ready=True):
    return {
        "metadata": {
            "name": name,
            "uid": uid,
            "creation_timestamp": created or NOW - timedelta(minutes=20),
            "owner_references": [_owner("ReplicaSet", rs_uid)] if rs_uid else [],
            "labels": {"app": "catalog"},
        },
        "spec": {
            "containers": [
                {
                    "name": "app",
                    "resources": {
                        "limits": {"memory": "128Mi", "cpu": "250m"},
                    },
                }
            ]
        },
        "status": {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": str(ready)}],
            "container_statuses": [
                {
                    "name": "app",
                    "restart_count": count,
                    "ready": ready,
                    "state": state or {"running": {}},
                }
            ],
        },
    }


def _usage(uid, *, cpu="200m", memory="107Mi", timestamp=None):
    return {
        "metadata": {"name": "catalog-1", "uid": uid},
        "timestamp": (timestamp or NOW - timedelta(seconds=10)).isoformat(),
        "window": "15s",
        "containers": [{"name": "app", "usage": {"cpu": cpu, "memory": memory}}],
    }


def _event(name, reason, *, last=None, created=None, type_="Warning", series=None):
    return {
        "metadata": {"name": name, "creation_timestamp": created},
        "type": type_,
        "reason": reason,
        "message": "示例说明",
        "last_timestamp": last,
        "series": {"last_observed_time": series, "count": 3} if series else None,
        "involved_object": {"kind": "Pod", "name": "catalog-1", "uid": "example"},
    }


class FakeKube:
    def __init__(self):
        self.sources = {
            "deployments": [_deployment("deploy-1", "catalog"), _deployment("deploy-2", "cart")],
            "replicasets": [
                _replicaset("rs-1", "deploy-1", 3, old="1", cause="vigil rollback act-fixture"),
                _replicaset("rs-2", "deploy-2", 2),
                _replicaset("rs-old-owner", "stale-uid", 4),
                _replicaset("rs-pruned-window", "deploy-1", 4, created=NOW - timedelta(days=2)),
            ],
            "pods": [_pod("pod-1", "rs-1")],
            "services": [
                {"metadata": {"name": "catalog"}, "spec": {"selector": {"app": "catalog"}}},
                {
                    "metadata": {"name": "catalog-external"},
                    "spec": {"selector": {"app": "catalog"}},
                },
            ],
            "events": [
                _event(
                    "backoff",
                    "BackOff",
                    last=NOW - timedelta(hours=2),
                    series=NOW - timedelta(minutes=3),
                ),
                _event("old", "Unhealthy", last=NOW - timedelta(hours=2)),
                _event("normal", "Created", last=NOW, type_="Normal"),
                _event("singleton", "FailedScheduling", created=NOW - timedelta(minutes=4)),
            ],
            "metrics": [_usage("pod-1")],
        }

    def monitoring_items(self, namespace, resource, *, timeout):
        assert namespace == "vigil-shop" and timeout > 0
        value = self.sources[resource]
        if value is None:
            raise RuntimeError("fixture-credential /home/sample/private")
        return value


def _payload(kind, result):
    return httpx.Response(
        200,
        json={
            "status": "success",
            "data": {
                "resultType": kind,
                "result": result,
            },
        },
    )


def _probe(signal, points):
    return {
        "metric": {"application": APP, "address": ADDRESS, "signal": signal},
        "values": [[time, str(value)] for time, value in points],
    }


def _node(node, **labels):
    return {"metric": {"node": node} | labels, "value": [SLOT, "1"]}


def _alert(fingerprint, state="active", ended=None):
    return {
        "status": {"state": state},
        "fingerprint": fingerprint,
        "startsAt": (NOW - timedelta(minutes=5)).isoformat(),
        "endsAt": (ended or NOW + timedelta(minutes=5)).isoformat(),
        "labels": {"severity": "critical", "private": "fixture-credential"},
        "annotations": {"summary": "fixture-credential"},
    }


class FakeHttp:
    def __init__(self, monkeypatch):
        self.errors = set()
        self.probes = [
            _probe("up", [(SLOT - 45, 1), (SLOT - 30, 0), (SLOT - 15, 1), (SLOT, 1)]),
            _probe("success", [(SLOT - 45, 1), (SLOT - 30, 1), (SLOT, 0)]),
            _probe("http_status", [(SLOT, 500)]),
            _probe("duration", [(SLOT, 0.01)]),
        ]
        self.restarts = []
        self.nodes = [
            _node("lab-0"),
            _node("lab-0", condition="Ready", status="true"),
            _node("lab-0", condition="MemoryPressure", status="false"),
            _node("lab-0", condition="DiskPressure", status="false"),
            _node("lab-0", condition="PIDPressure", status="false"),
        ]
        client = httpx.AsyncClient
        transport = httpx.MockTransport(self.send)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(transport=transport, **kw))

    def send(self, request):
        assert request.method == "GET"
        query = request.url.params.get("query", "")
        match request.url.path:
            case "/api/v2/alerts/groups":
                if "alerts" in self.errors:
                    return httpx.Response(503, text="fixture-credential")
                return httpx.Response(
                    200,
                    json=[
                        {
                            "labels": {"alertname": "FrontendProbeFailed"},
                            "receiver": {"name": "vigil-webhook"},
                            "alerts": [
                                _alert("a"),
                                _alert("b", state="suppressed"),
                                _alert("resolved", ended=NOW - timedelta(seconds=1)),
                            ],
                        }
                    ],
                )
            case "/api/v1/query_range":
                assert 'job="vigil-entry-probe"' in query and "frontend-probe" not in query
                assert request.url.params["step"] == "15s"
                assert float(request.url.params["end"]) == SLOT
                return _payload("matrix", self.probes)
            case "/api/v1/query" if query == NODE_QUERY:
                return _payload("vector", self.nodes)
            case "/api/v1/query":
                assert 'namespace="vigil-shop"' in query and "increase(" in query
                return _payload("vector", self.restarts)
            case _:
                raise AssertionError(request.url)


async def _rollbacks(start, end):
    assert end == NOW and start == NOW - timedelta(hours=24)
    return [
        RollbackRecord(
            "act-fixture",
            "deploy-1",
            "catalog",
            1,
            "shop:v1",
            NOW - timedelta(minutes=10),
            "实验审批人",
        ),
        RollbackRecord(
            "act-pruned",
            "deploy-1",
            "catalog",
            20,
            "shop:v20",
            NOW - timedelta(minutes=5),
            "实验审批人",
        ),
        RollbackRecord(
            "act-stale",
            "stale-uid",
            "catalog",
            1,
            "shop:v1",
            NOW - timedelta(minutes=3),
            "实验审批人",
        ),
    ]


@pytest.fixture
def example(monkeypatch):
    http = FakeHttp(monkeypatch)
    kube = FakeKube()
    monitoring = Monitoring(
        kube,
        namespace="vigil-shop",
        prometheus_url="http://prom.example",
        alertmanager_url="http://am.example",
        grafana_url="https://grafana.example/lab/",
        rollbacks=_rollbacks,
    )
    return monitoring, kube, http


def test_six_panels_match_contract(example):
    monitoring, _, _ = example
    snapshot = asyncio.run(monitoring.snapshot(now=NOW))
    assert snapshot["alerts"]["status"] == "ok"
    (group,) = snapshot["alerts"]["data"]["groups"]
    webhook_example = {
        "groupKey": '{}/{alertname="FrontendProbeFailed"}:{alertname="FrontendProbeFailed"}'
    }
    assert group["group_key"] == webhook_example["groupKey"]
    assert group["session"] is None
    assert [(a["fingerprint"], a["suppressed"]) for a in group["alerts"]] == [
        ("a", False),
        ("b", True),
    ]
    assert group["alerts"][0]["duration_seconds"] == 300
    assert "fixture-credential" not in str(snapshot)

    assert snapshot["probes"]["status"] == "partial"
    (application,) = snapshot["probes"]["data"]["applications"]
    (target,) = application["targets"]
    assert application["application"] == APP and target["address"] == ADDRESS
    assert target["http_status"] == 500 and target["duration_seconds"] == 0.01
    assert target["state"] == "down" and target["availability"] == 0.5
    assert len(target["points"]) == 240 and target["sample_count"] == 2
    assert target["points"][-4:] == [
        [SLOT - 45, 1.0],
        [SLOT - 30, None],
        [SLOT - 15, None],
        [SLOT, 0.0],
    ]

    rows = snapshot["changes"]["data"]["items"]
    rollback, release, cart = rows
    assert rollback["kind"] == "vigil_rollback" and rollback["version"] == "r1"
    assert rollback["approved_by"] == "实验审批人"
    assert (release["kind"], release["service"], release["version"]) == ("release", "catalog", "r1")
    assert cart["service"] == "cart"
    assert all("approved_by" not in row for row in (release, cart))

    assert snapshot["abnormal_service_count"] == 1
    health = snapshot["services"]["data"]
    assert health["healthy"] == [{"name": "cart", "kind": "Deployment"}]
    (catalog,) = health["abnormal"]
    (issue,) = catalog["issues"]
    assert issue["resource"] == "memory" and issue["ratio"] == pytest.approx(107 / 128)

    assert [row["reason"] for row in snapshot["warnings"]["data"]["items"]] == [
        "BackOff",
        "FailedScheduling",
    ]
    assert snapshot["nodes"]["data"] == {"healthy": ["lab-0"], "abnormal": [], "unknown": []}
    assert snapshot["grafana_links"]["alerts"] == "https://grafana.example/lab/d/vigil-alerts"
    assert len(snapshot["grafana_links"]) == 5 and "warnings" not in snapshot["grafana_links"]
    assert grafana_links(None) == {}


def test_dependency_failures_keep_other_panels_and_confirmed_count(example):
    monitoring, kube, http = example
    kube.sources["events"] = None
    kube.sources["metrics"] = None
    kube.sources["deployments"][0]["status"]["ready_replicas"] = 0
    http.errors.add("alerts")
    http.probes = []
    http.nodes = []

    snapshot = asyncio.run(monitoring.snapshot(now=NOW))
    assert snapshot["alerts"] == {
        "status": "error",
        "data": None,
        "missing": [],
        "failed": ["alertmanager"],
    }
    assert snapshot["warnings"]["failed"] == ["events"]
    assert snapshot["nodes"]["status"] == snapshot["probes"]["status"] == "missing"
    assert snapshot["abnormal_service_count"] == 1
    assert snapshot["services"]["status"] == "partial"
    assert snapshot["services"]["missing"] == ["container_usage"]
    assert snapshot["services"]["failed"] == ["metrics"]
    assert "fixture-credential" not in str(snapshot)


@pytest.mark.parametrize("cause", ["wrong_uid", "predates_pod", "stale", "unavailable"])
def test_resource_missing_does_not_mark_service_healthy(example, cause):
    monitoring, kube, _ = example
    if cause == "wrong_uid":
        kube.sources["metrics"] = [_usage("stale-uid")]
    elif cause == "predates_pod":
        kube.sources["pods"][0]["metadata"]["creation_timestamp"] = NOW - timedelta(seconds=8)
    elif cause == "stale":
        kube.sources["metrics"] = [_usage("pod-1", timestamp=NOW - timedelta(minutes=3))]
    else:
        kube.sources["metrics"] = []
    snapshot = asyncio.run(monitoring.snapshot(now=NOW))
    assert snapshot["abnormal_service_count"] is None
    (service,) = snapshot["services"]["data"]["unknown"]
    assert service["resource_status"] == "missing"
    assert service["name"] == "catalog"


def test_pod_identity_and_ambiguous_service_selectors(example):
    monitoring, kube, _ = example
    kube.sources["pods"] = [_pod("pod-1", "rs-from-deleted-deployment", count=4)]
    snapshot = asyncio.run(monitoring.snapshot(now=NOW))
    assert snapshot["abnormal_service_count"] is None
    assert "pod_ownership" in snapshot["services"]["missing"]
    assert snapshot["services"]["data"]["abnormal"] == []
    kube.sources["pods"] = [_pod("pod-1", None, count=4)]
    kube.sources["deployments"] = []
    snapshot = asyncio.run(monitoring.snapshot(now=NOW))
    assert {row["name"] for row in snapshot["services"]["data"]["unknown"]} == {
        "catalog",
        "catalog-external",
    }

    kube.sources["services"].pop()
    snapshot = asyncio.run(monitoring.snapshot(now=NOW))
    (service,) = snapshot["services"]["data"]["abnormal"]
    assert service["kind"] == "Service"
    assert service["name"] == "catalog"
    assert {issue["kind"] for issue in service["issues"]} == {
        "frequent_restarts",
        "resource_near_limit",
    }


def test_restart_query_uses_pod_uid_and_reports_exit(example):
    monitoring, kube, http = example
    pod = kube.sources["pods"][0]
    pod["metadata"]["creation_timestamp"] = NOW - timedelta(hours=4)
    status = pod["status"]["container_statuses"][0]
    status["restart_count"] = 4
    status["state"] = {"waiting": {"reason": "CrashLoopBackOff"}}
    status["last_state"] = {
        "terminated": {
            "reason": "OOMKilled",
            "exit_code": 137,
            "finished_at": NOW - timedelta(minutes=2),
        }
    }
    http.restarts = [
        {"metric": {"pod": "catalog-1", "uid": "pod-1", "container": "app"}, "value": [SLOT, "3"]},
        {"metric": {"pod": "catalog-1", "uid": "stale", "container": "app"}, "value": [SLOT, "42"]},
    ]
    snapshot = asyncio.run(monitoring.snapshot(now=NOW))
    (service,) = snapshot["services"]["data"]["abnormal"]
    assert service["resource_status"] == "not_applicable"
    assert service["last_exits"][0]["reason"] == "OOMKilled"
    assert service["issues"][1]["reason"] == "CrashLoopBackOff"
    assert service["issues"][2]["restart_count_last_hour"] == 3


def test_probe_all_slots_missing_and_node_pressure_unknown(example):
    monitoring, _, http = example
    http.probes = [_probe("up", [(SLOT, 0)])]
    http.nodes += [_node("lab-1"), _node("lab-1", condition="MemoryPressure", status="true")]
    snapshot = asyncio.run(monitoring.snapshot(now=NOW))
    (target,) = snapshot["probes"]["data"]["applications"][0]["targets"]
    assert target["availability"] is None and target["state"] == "missing"
    assert all(value is None for _, value in target["points"])
    assert snapshot["nodes"]["status"] == "partial"
    assert snapshot["nodes"]["data"]["abnormal"] == [
        {"name": "lab-1", "conditions": [{"condition": "MemoryPressure", "status": "true"}]},
    ]
    assert snapshot["nodes"]["data"]["healthy"] == ["lab-0"]


def test_release_inheriting_old_change_cause_retains_its_own_revision(example):
    _, kube, _ = example
    from aiops.monitoring.releases import changes

    inherited = _replicaset(
        "rs-new",
        "deploy-1",
        4,
        cause="vigil rollback act-fixture",
        created=NOW - timedelta(minutes=2),
    )
    unverified = _replicaset(
        "rs-unknown",
        "deploy-2",
        5,
        cause="vigil rollback act-unverified",
        created=NOW - timedelta(minutes=1),
    )
    kube.sources["replicasets"].extend([inherited, unverified])
    records = asyncio.run(_rollbacks(NOW - timedelta(hours=24), NOW))
    change = changes(
        "vigil-shop", kube.sources["deployments"], kube.sources["replicasets"], records, NOW
    )
    assert change["status"] == "partial"
    assert change["missing"] == ["rollback_classification"]
    assert [(row["kind"], row["version"]) for row in change["data"]["items"]] == [
        ("release", "r4"),
        ("vigil_rollback", "r1"),
        ("release", "r1"),
        ("release", "r2"),
    ]
