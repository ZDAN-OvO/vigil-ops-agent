import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import jsonpatch
import pytest
from kubernetes.client import ApiClient, Configuration
from urllib3.response import HTTPResponse

from aiops.actions.policy import CurrentState
from aiops.actions.remediation_kube import (
    RECONCILIATION_ANNOTATION,
    PatchPlan,
    RemediationKubeAdapter,
    RemediationKubeError,
    Snapshot,
    WriteReceipt,
    WriteRejected,
    WriteUnknown,
)
from aiops.actions.remediation_observation import RemediationMetrics
from aiops.actions.remediation_policy import EvidencePredicate
from aiops.actions.remediation_runtime import RemediationRuntime
from aiops.contracts import ActionTarget, AdjustResourcesParams

WORKLOAD = ActionTarget(
    cluster="lab",
    namespace="shop",
    kind="Deployment",
    name="web",
    uid="deployment-uid",
    resource_version="1",
)
CONFIG_MAP = ActionTarget(
    cluster="lab",
    namespace="shop",
    kind="ConfigMap",
    name="settings",
    uid="config-map-uid",
    resource_version="1",
)
CONFIG_PARAMS = {"source": "config_map", "key": "sample", "value": "new", "config_map": "settings"}
ENV_PARAMS = {"source": "env", "container": "app", "key": "SAMPLE", "value": "new"}
RESOURCE_PARAMS = {"container": "app", "limit_memory": "128Mi"}


def document(kind, name, uid, *, version="1", group="apps/v1", **fields):
    return {
        "apiVersion": group,
        "kind": kind,
        "metadata": {"name": name, "namespace": "shop", "uid": uid, "resourceVersion": version},
        **fields,
    }


def deployment():
    result = document(
        "Deployment",
        "web",
        WORKLOAD.uid,
        spec={
            "replicas": 1,
            "template": {
                "metadata": {"labels": {"app": "web"}},
                "spec": {
                    "containers": [
                        {
                            "name": "app",
                            "image": "app:v1",
                            "env": [{"name": "SAMPLE", "value": "old"}],
                        }
                    ]
                },
            },
        },
        status={
            "observedGeneration": 1,
            "replicas": 1,
            "updatedReplicas": 1,
            "readyReplicas": 1,
            "availableReplicas": 1,
        },
    )
    result["metadata"]["generation"] = 1
    return result


def ref(kind, name, uid):
    return {"apiVersion": "apps/v1", "kind": kind, "name": name, "uid": uid, "controller": True}


@pytest.mark.parametrize(
    ("kind", "parameters", "original"),
    [
        (
            "adjust_resources",
            {"container": "app", "limit_memory": "128Mi"},
            {"container": "app", "limit_memory": "16Mi"},
        ),
        (
            "adjust_resources",
            {"container": "app", "limit_cpu": "200m"},
            {"container": "app", "limit_cpu": "5m", "limit_memory": "16Mi"},
        ),
        ("scale_replicas", {"replicas": 2}, {"replicas": 1}),
    ],
)
def test_runtime_preserves_field_location_and_optional_current_resources(
    kind,
    parameters,
    original,
):
    cluster = Cluster()
    limits = {"memory": "16Mi"}
    if "limit_cpu" in parameters:
        limits["cpu"] = "5m"
    cluster.app["resources"] = {"limits": limits}
    snapshot = cluster.adapter.snapshot(WORKLOAD, kind, parameters)
    runtime = RemediationRuntime(None, None, None, cluster.adapter, None)
    current = asyncio.run(runtime._current(snapshot, WORKLOAD))
    assert current.parameters.model_dump(exclude_none=True) == original


class Cluster:
    def __init__(self, *, exclusive=True):
        self.objects = {
            "deployments": {"web": deployment()},
            "configmaps": {
                "settings": document(
                    "ConfigMap",
                    "settings",
                    CONFIG_MAP.uid,
                    group="v1",
                    data={"sample": "old", "untouched": "private"},
                ),
            },
        }
        self.calls = []
        self.patches = []
        self.error = None
        self.read_error = None
        self.next_version = 2
        self.hook = lambda result: result
        self.read_client = ApiClient(Configuration(host="https://example.invalid"))
        self.write_client = ApiClient(Configuration(host="https://example.invalid"))
        self.read_client.rest_client.pool_manager.request = self.request
        self.write_client.rest_client.pool_manager.request = self.request
        self.adapter = RemediationKubeAdapter(
            read_client=self.read_client,
            write_client=self.write_client,
            is_registered=lambda target: (
                (target.cluster, target.namespace, target.kind, target.name)
                in {
                    ("lab", "shop", "Deployment", "web"),
                    ("lab", "shop", "ConfigMap", "settings"),
                    ("lab", "shop", "StatefulSet", "web"),
                }
            ),
            config_map_exclusive=lambda *_: exclusive,
        )

    @property
    def workload(self):
        return self.objects["deployments"]["web"]

    @property
    def app(self):
        return self.workload["spec"]["template"]["spec"]["containers"][0]

    @property
    def config_map(self):
        return self.objects["configmaps"]["settings"]

    def request(self, method, url, *, headers, body=None, **kwargs):
        route = urlsplit(url)
        collection, *names = route.path.split("/namespaces/shop/")[1].split("/")
        name = names[0] if names else None
        objects = self.objects.get(collection, {})
        query = parse_qs(route.query)
        query.update(dict(kwargs.get("fields") or ()))
        self.calls.append((method, collection, name, query))
        if method == "GET":
            if self.read_error == collection:
                return self.response({"message": "secret error body"}, status=403)
            if name:
                if name in objects:
                    return self.response(objects[name])
                return self.response({"message": "not found"}, status=404)
            kinds = {
                "deployments": "Deployment",
                "statefulsets": "StatefulSet",
                "replicasets": "ReplicaSet",
                "daemonsets": "DaemonSet",
                "pods": "Pod",
                "podtemplates": "PodTemplate",
                "replicationcontrollers": "ReplicationController",
                "jobs": "Job",
                "cronjobs": "CronJob",
            }
            return self.response(
                {
                    "kind": kinds[collection] + "List",
                    "items": list(objects.values()),
                    "metadata": {"resourceVersion": str(self.next_version)},
                }
            )

        assert method == "PATCH" and name in objects
        assert headers["Content-Type"] == "application/json-patch+json"
        assert kwargs["retries"] is False and kwargs["redirect"] is False
        operations = json.loads(body)
        dry_run = query.get("dryRun") == ["All"]
        self.patches.append((dry_run, operations))
        if not dry_run and self.error is not None:
            if isinstance(self.error, BaseException):
                raise self.error
            return self.response({"message": "secret error body"}, status=self.error)
        previous = objects[name]
        try:
            result = jsonpatch.apply_patch(previous, operations)
        except jsonpatch.JsonPatchException:
            return self.response({"message": "conflict"}, status=422)
        if result.get("spec") != previous.get("spec"):
            result["metadata"]["generation"] += 1
        result = self.hook(result)
        if not dry_run:
            result["metadata"]["resourceVersion"] = str(self.next_version)
            self.next_version += 1
            objects[name] = result
        return self.response(result)

    @staticmethod
    def response(payload, status=200):
        return HTTPResponse(
            body=json.dumps(payload).encode(),
            status=status,
            headers={"Content-Type": "application/json"},
        )

    def plan(self, kind="adjust_resources", params=RESOURCE_PARAMS):
        snapshot = self.adapter.snapshot(WORKLOAD, kind, params)
        return self.adapter.prepare(snapshot)

    def config_snapshot(self):
        return self.adapter.snapshot(
            WORKLOAD,
            "update_config",
            CONFIG_PARAMS,
            config_map_target=CONFIG_MAP,
        )

    def config_reference(self):
        self.app["envFrom"] = [{"configMapRef": {"name": "settings"}}]


def assert_code(code, operation, *args, **kwargs):
    with pytest.raises(RemediationKubeError) as err:
        operation(*args, **kwargs)
    assert err.value.code == code


def test_resource_patch_round_trip_and_restore_only_written_fields():
    cluster = Cluster()
    adapter = cluster.adapter
    cluster.app["resources"] = {"requests": {"cpu": "250m", "memory": "64Mi"}}
    snap = adapter.snapshot(WORKLOAD, "adjust_resources", RESOURCE_PARAMS)
    snap = Snapshot.from_dict(json.loads(json.dumps(snap.to_dict())))
    assert adapter.current_parameters(snap) == {
        "request_cpu": "250m",
        "request_memory": "64Mi",
        "limit_cpu": None,
        "limit_memory": None,
    }
    plan = PatchPlan.from_dict(adapter.prepare(snap).to_dict())
    preflight = adapter.dry_run(plan)
    authorizations = []
    receipt = adapter.send(preflight, authorize=lambda: authorizations.append(len(cluster.patches)))
    assert authorizations == [1]
    assert cluster.patches == [
        (True, json.loads(plan.body_json)),
        (False, json.loads(plan.body_json)),
    ]
    assert cluster.patches[0][1][:2] == [
        {"op": "test", "path": "/metadata/uid", "value": WORKLOAD.uid},
        {"op": "test", "path": "/metadata/resourceVersion", "value": "1"},
    ]
    assert receipt == WriteReceipt.from_dict(receipt.to_dict())
    assert adapter.verify(plan, receipt).state == "pending"
    cluster.workload["status"]["observedGeneration"] = 2
    assert adapter.verify(plan, receipt).state == "ready"
    assert adapter.inspect(plan).state == "after"

    cluster.app["resources"]["limits"]["cpu"] = "500m"
    cluster.workload["metadata"]["resourceVersion"] = "9"
    restore = adapter.prepare_restore(plan)
    assert json.loads(restore.body_json)[1]["value"] == "9"
    adapter.send(adapter.dry_run(restore), authorize=lambda: None)
    assert cluster.app["resources"]["limits"] == {"cpu": "500m"}
    assert adapter.inspect(plan).state == "before"


def test_restore_drops_maps_created_by_forward_patch():
    cluster = Cluster()
    plan = cluster.plan()
    cluster.adapter.send(cluster.adapter.dry_run(plan), authorize=lambda: None)
    restore = cluster.adapter.prepare_restore(plan)
    assert [item["path"] for item in json.loads(restore.body_json) if item["op"] == "remove"] == [
        "/spec/template/spec/containers/0/resources/limits/memory",
        "/spec/template/spec/containers/0/resources/limits",
        "/spec/template/spec/containers/0/resources",
    ]
    cluster.adapter.send(cluster.adapter.dry_run(restore), authorize=lambda: None)
    assert "resources" not in cluster.app


def test_target_lookup_uses_registered_name_and_live_identity():
    cluster = Cluster()
    assert cluster.adapter.target("lab", "shop", "Deployment", "web") == WORKLOAD
    assert_code("target_not_registered", cluster.adapter.target, "lab", "shop", "Deployment", "db")
    assert len(cluster.calls) == 1
    cluster.workload["metadata"]["namespace"] = "other"
    assert_code("drift", cluster.adapter.target, "lab", "shop", "Deployment", "web")


def test_scale_statefulset_requires_current_revision_and_healthy_counts():
    cluster = Cluster()
    ss = deployment()
    ss["kind"] = "StatefulSet"
    ss["status"].update({"currentRevision": "old", "currentReplicas": 1, "updateRevision": "rev"})
    cluster.objects["statefulsets"] = {"web": ss}
    target = WORKLOAD.model_copy(update={"kind": "StatefulSet"})
    adapter = cluster.adapter
    plan = adapter.prepare(adapter.snapshot(target, "scale_replicas", {"replicas": 2}))
    receipt = adapter.send(adapter.dry_run(plan), authorize=lambda: None)
    status = cluster.objects["statefulsets"]["web"]["status"]
    status.update(
        {
            "replicas": 2,
            "currentReplicas": 2,
            "updatedReplicas": 2,
            "readyReplicas": 2,
            "availableReplicas": 2,
            "observedGeneration": 2,
        }
    )
    assert adapter.verify(plan, receipt).state == "pending"
    status["currentRevision"] = "rev"
    assert adapter.verify(plan, receipt).state == "ready"
    cluster.objects["statefulsets"]["web"]["metadata"]["generation"] += 1
    assert adapter.verify(plan, receipt).state == "drift"


@pytest.mark.parametrize(
    ("kind", "params"),
    [
        ("scale_replicas", {"replicas": True}),
        ("scale_replicas", {"replicas": 0}),
        ("scale_replicas", {}),
        ("adjust_resources", {"container": "app"}),
        ("adjust_resources", {"container": "app", "request_cpu": 1}),
        ("update_config", {"source": [], "key": "x", "value": "x"}),
        ("update_config", {"source": "env", "key": "SAMPLE", "value": "x"}),
    ],
)
def test_bad_parameters_are_rejected_before_request(kind, params):
    cluster = Cluster()
    with pytest.raises(RemediationKubeError):
        cluster.adapter.snapshot(WORKLOAD, kind, params)
    assert cluster.calls == []


def test_same_quantity_and_preflight_drift_avoid_send():
    cluster = Cluster()
    cluster.app["resources"] = {"limits": {"memory": "128Mi"}}
    snapshot = cluster.adapter.snapshot(
        WORKLOAD,
        "adjust_resources",
        {
            "container": "app",
            "limit_memory": "134217728",
        },
    )
    assert_code("no_change", cluster.adapter.prepare, snapshot)
    plan = cluster.plan("scale_replicas", {"replicas": 2})
    preflight = cluster.adapter.dry_run(plan)
    cluster.workload["metadata"]["resourceVersion"] = "2"
    assert_code("drift", cluster.adapter.send, preflight, authorize=lambda: pytest.fail("授权"))
    assert [is_dry for is_dry, _ in cluster.patches] == [True]


def test_env_accepts_literal_empty_and_rejects_value_from_and_missing_name():
    cluster = Cluster()
    cluster.app["env"] = [{"name": "SAMPLE"}]
    plan = cluster.plan("update_config", ENV_PARAMS)
    assert plan.before.fields == {"value": ""}
    cluster.adapter.send(cluster.adapter.dry_run(plan), authorize=lambda: None)
    assert cluster.app["env"] == [{"name": "SAMPLE", "value": "new"}]
    restore = cluster.adapter.prepare_restore(plan)
    cluster.adapter.send(cluster.adapter.dry_run(restore), authorize=lambda: None)
    assert cluster.app["env"] == [{"name": "SAMPLE"}]
    cluster.app["env"] = [{"name": "SAMPLE", "valueFrom": {"secretKeyRef": {"name": "hidden"}}}]
    assert_code("config_value_unavailable", cluster.plan, "update_config", ENV_PARAMS)
    cluster.app["env"] = []
    assert_code("config_value_unavailable", cluster.plan, "update_config", ENV_PARAMS)


@pytest.mark.parametrize(
    ("failure", "error_type", "status"),
    [
        (422, WriteRejected, 422),
        (403, WriteRejected, 403),
        (500, WriteUnknown, 500),
        (504, WriteUnknown, 504),
        (TimeoutError("secret transport detail"), WriteUnknown, None),
        (asyncio.CancelledError("secret cancellation detail"), WriteUnknown, None),
    ],
)
def test_send_classifies_rejection_and_unknown_without_leaks(failure, error_type, status, caplog):
    cluster = Cluster()
    preflight = cluster.adapter.dry_run(cluster.plan())
    cluster.error = failure
    with caplog.at_level("DEBUG", logger="kubernetes.client.rest"):
        with pytest.raises(error_type) as err:
            cluster.adapter.send(preflight, authorize=lambda: None)
    assert err.value.status == status
    assert "secret" not in str(err.value)
    assert "secret" not in caplog.text
    assert [is_dry for is_dry, _ in cluster.patches] == [True, False]


def test_send_requires_unmodified_dry_run_and_grant():
    cluster = Cluster()
    adapter = cluster.adapter
    plan = cluster.plan("scale_replicas", {"replicas": 2})
    flight = adapter.dry_run(plan)
    flight.plan.after["replicas"] = 3
    assert_code("invalid_preflight", adapter.send, flight, authorize=lambda: pytest.fail("授权"))
    flight = adapter.dry_run(plan)

    def denied():
        raise PermissionError

    with pytest.raises(PermissionError):
        adapter.send(flight, authorize=denied)
    assert all(dry for dry, _ in cluster.patches)


def test_dry_run_rejects_mutating_admission_outside_target_field():
    cluster = Cluster()
    plan = cluster.plan("scale_replicas", {"replicas": 2})

    def hook(result):
        result["spec"]["template"]["spec"]["containers"][0]["image"] = "changed:v1"
        return result

    cluster.hook = hook
    assert_code("unexpected_write_spec", cluster.adapter.dry_run, plan)


def test_config_map_requires_exclusive_policy_and_keeps_other_keys():
    cluster = Cluster(exclusive=False)
    cluster.config_reference()
    assert_code("config_map_exclusivity_unknown", cluster.config_snapshot)

    cluster = Cluster()
    cluster.config_reference()
    adapter = cluster.adapter
    snapshot = cluster.config_snapshot()
    assert adapter.current_parameters(snapshot) == {"value": "old"}
    assert "private" not in json.dumps(snapshot.to_dict())
    plan = adapter.prepare(snapshot)
    receipt = adapter.send(adapter.dry_run(plan), authorize=lambda: None)
    assert adapter.verify(plan, receipt).state == "ready"
    assert cluster.config_map["data"] == {"sample": "new", "untouched": "private"}
    restore = adapter.prepare_restore(plan)
    adapter.send(adapter.dry_run(restore), authorize=lambda: None)
    assert adapter.inspect(plan).state == "before"


@pytest.mark.parametrize(
    "kind", ["Deployment", "Pod", "CronJob", "Job", "PodTemplate", "DaemonSet"]
)
def test_config_map_rejects_other_consumers_including_inactive_templates(kind):
    cluster = Cluster()
    cluster.config_reference()
    spec = {
        "containers": [],
        "volumes": [
            {
                "name": "config",
                "projected": {
                    "sources": [
                        {"configMap": {"name": "settings", "optional": True}},
                    ]
                },
            }
        ],
    }
    template = {"spec": spec}
    collections = {
        "Deployment": ("deployments", {"spec": {"replicas": 0, "template": template}}),
        "DaemonSet": ("daemonsets", {"spec": {"template": template}}),
        "Job": ("jobs", {"spec": {"template": template}}),
        "CronJob": ("cronjobs", {"spec": {"jobTemplate": {"spec": {"template": template}}}}),
        "PodTemplate": ("podtemplates", {"template": template}),
        "Pod": ("pods", {"spec": spec}),
    }
    collection, body = collections[kind]
    cluster.objects.setdefault(collection, {})["foreign"] = document(
        kind,
        "foreign",
        "foreign-uid",
        **body,
    )
    assert_code("config_map_shared", cluster.config_snapshot)
    assert cluster.patches == []


def test_config_map_fails_closed_if_consumer_list_forbidden():
    cluster = Cluster()
    cluster.config_reference()
    cluster.read_error = "cronjobs"
    with pytest.raises(RemediationKubeError) as err:
        cluster.config_snapshot()
    assert err.value.status == 403
    assert "secret" not in str(err.value)


def test_isolation_fences_old_send_and_confirms_marker_on_actual_object():
    cluster = Cluster()
    adapter = cluster.adapter
    plan = cluster.plan()
    original = plan.before.object
    assert adapter.isolate(original, "audit-1") == "2"
    assert adapter.confirm_isolation(original, "audit-1") == "2"
    assert adapter.isolate(original, "audit-1") == "2"
    assert len(cluster.patches) == 1
    assert cluster.workload["metadata"]["annotations"] == {RECONCILIATION_ANNOTATION: "audit-1"}
    assert adapter.inspect(plan).state == "before"
    assert_code("drift", adapter.dry_run, plan)
    assert_code("isolation_unconfirmed", adapter.confirm_isolation, original, "other")
    assert_code(
        "target_not_registered",
        adapter.isolate,
        original.model_copy(update={"kind": "Pod"}),
        "audit-2",
    )


def test_deployment_pod_evidence_checks_owner_uid_template_and_age():
    cluster = Cluster()
    plan = cluster.plan()
    receipt = cluster.adapter.send(cluster.adapter.dry_run(plan), authorize=lambda: None)
    cluster.workload["status"]["observedGeneration"] = receipt.generation
    template = deepcopy(cluster.workload["spec"]["template"])
    template["metadata"]["labels"]["pod-template-hash"] = "current"
    rs = document("ReplicaSet", "web-rs", "rs-uid", spec={"template": template})
    rs["metadata"]["ownerReferences"] = [ref("Deployment", WORKLOAD.name, WORKLOAD.uid)]
    stale_rs = deepcopy(rs)
    stale_rs["metadata"].update({"name": "stale-rs", "uid": "stale-uid"})
    stale_rs["spec"]["template"]["spec"]["containers"][0]["image"] = "app:old"
    cluster.objects["replicasets"] = {"web-rs": rs, "stale-rs": stale_rs}

    def pod(name, owner, *, finished="2026-11-27T10:00:00Z"):
        result = document(
            "Pod",
            name,
            f"{name}-uid",
            group="v1",
            spec=deepcopy(template["spec"]),
            status={
                "containerStatuses": [
                    {
                        "name": "app",
                        "containerID": "containerd://current",
                        "restartCount": 3,
                        "lastState": {
                            "terminated": {
                                "reason": "OOMKilled",
                                "finishedAt": finished,
                                "containerID": "containerd://x",
                            }
                        },
                    }
                ]
            },
        )
        result["metadata"].update(
            {
                "labels": {"app": "web", "pod-template-hash": "current"},
                "ownerReferences": [owner],
            }
        )
        return result

    cluster.objects["pods"] = {
        "current": pod("current", ref("ReplicaSet", "web-rs", "rs-uid")),
        "forged": pod("forged", ref("ReplicaSet", "web-rs", "other-uid")),
        "old": pod("old", ref("ReplicaSet", "stale-rs", "stale-uid")),
        "old-event": pod(
            "old-event",
            ref("ReplicaSet", "web-rs", "rs-uid"),
            finished="2026-11-27T09:58:00Z",
        ),
    }
    assert cluster.adapter.current_pods(WORKLOAD, "app") == ("current", "old-event")
    (evidence,) = cluster.adapter.pod_evidence(
        WORKLOAD,
        "app",
        now=datetime(2026, 11, 27, 10, 0, 30, tzinfo=UTC),
        max_age_seconds=60,
    )
    assert (evidence.pod_uid, evidence.replica_set_uid, evidence.restart_count) == (
        "current-uid",
        "rs-uid",
        3,
    )
    assert_code(
        "observation_pods_unconfirmed", cluster.adapter.observation_pods, plan, receipt, "app"
    )
    cluster.objects["pods"].pop("old-event")
    (current,) = cluster.adapter.observation_pods(plan, receipt, "app")
    assert (current.uid, current.container_id, current.restarts, current.oom_finished_at) == (
        "current-uid",
        "containerd://current",
        3,
        (datetime(2026, 11, 27, 10, tzinfo=UTC),),
    )
    cluster.objects["pods"]["current"]["status"]["containerStatuses"][0]["lastState"]["terminated"][
        "finishedAt"
    ] = "2026-11-27T10:00:00"
    assert_code("oom_timestamp_unconfirmed", cluster.adapter.observation_pods, plan, receipt, "app")


@pytest.mark.parametrize(
    "failure",
    [None, "stale_total", "stale_throttled", "missing_rate", "zero", "unconfirmed_id"],
)
def test_cpu_rule_queries_only_current_running_container_counters(failure):
    cluster = Cluster()
    template = deepcopy(cluster.workload["spec"]["template"])
    template["metadata"]["labels"]["pod-template-hash"] = "abc"
    rs = document("ReplicaSet", "web-abc", "rs-uid", spec={"template": template})
    rs["metadata"]["ownerReferences"] = [ref("Deployment", WORKLOAD.name, WORKLOAD.uid)]
    cluster.objects["replicasets"] = {"web-abc": rs}

    def pod(name, container_id, state):
        result = document(
            "Pod",
            name,
            f"{name}-uid",
            group="v1",
            spec=template["spec"],
            status={
                "containerStatuses": [
                    {
                        "name": "app",
                        "containerID": f"containerd://{container_id}",
                        "state": state,
                        "lastState": {"terminated": {"containerID": f"containerd://{'c' * 64}"}},
                    },
                ]
            },
        )
        result["metadata"].update(
            {
                "labels": template["metadata"]["labels"],
                "ownerReferences": [ref("ReplicaSet", "web-abc", "rs-uid")],
            }
        )
        return result

    cluster.objects["pods"] = {
        "web-abc-1": pod("web-abc-1", "a" * 64, {"running": {}}),
        "web-abc-2": pod("web-abc-2", "b" * 64, {"running": {}}),
        "web-abc-waiting": pod("web-abc-waiting", "c" * 64, {"waiting": {}}),
    }
    if failure == "unconfirmed_id":
        del cluster.objects["pods"]["web-abc-2"]["status"]["containerStatuses"][0]["containerID"]
    at = datetime(2026, 11, 27, 10, tzinfo=UTC)
    now = at.timestamp()
    answers = {}
    for pod_name, container_id, periods, throttled in (
        ("web-abc-1", "a" * 64, 10, 9),
        ("web-abc-2", "b" * 64, 30, 3),
    ):
        labels = (
            f'job="kubelet-cadvisor",namespace="shop",container="app",pod="{pod_name}",'
            f'id=~"(.*/)?([^/]+-)?{container_id}([.]scope)?"'
        )
        for metric, rate, stale in (
            ("container_cpu_cfs_periods_total", periods, "stale_total"),
            ("container_cpu_cfs_throttled_periods_total", throttled, "stale_throttled"),
        ):
            selector = f"{metric}{{{labels}}}"
            age = 61 if failure == stale and pod_name == "web-abc-2" else 15
            answers[f"timestamp({selector})"] = now - age
            if (
                failure == "missing_rate"
                and pod_name == "web-abc-2"
                and metric.endswith("throttled_periods_total")
            ):
                rate = None
            answers[f"rate({selector}[1m])"] = 0 if failure == "zero" else rate

    queries = set()

    def prometheus(request):
        assert request.url.path == "/api/v1/query"
        assert float(request.url.params["time"]) == now
        query = request.url.params["query"]
        queries.add(query)
        if query not in answers:
            return httpx.Response(400, json={"error": "unexpected PromQL selector"})
        value = answers[query]
        result = [] if value is None else [{"value": [now, str(value)]}]
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "vector", "result": result}}
        )

    async def evaluate():
        transport = httpx.MockTransport(prometheus)
        async with httpx.AsyncClient(base_url="http://prometheus", transport=transport) as client:
            runtime = RemediationRuntime(
                None, None, None, cluster.adapter, RemediationMetrics(client)
            )
            current = CurrentState(
                target=WORKLOAD, parameters=AdjustResourcesParams(container="app", limit_cpu="5m")
            )
            rule = SimpleNamespace(
                evidence=[
                    EvidencePredicate(
                        key="throttle_ratio", operator="ge", value=0.5, promql="unscoped_ratio"
                    )
                ],
                max_evidence_age_seconds=90,
            )
            return await runtime._metrics_evidence(rule, current, at)

    evidence = asyncio.run(evaluate())
    if failure is None:
        assert evidence["throttle_ratio"] == pytest.approx(0.3)
        assert evidence["prometheus_sample_times"] == {"throttle_ratio": now - 15}
    else:
        assert "throttle_ratio" not in evidence
        assert evidence["prometheus_sample_times"] == {}
    if failure == "unconfirmed_id":
        assert not queries
    elif failure in {"stale_total", "stale_throttled"}:
        assert queries == {query for query in answers if query.startswith("timestamp(")}
    else:
        assert queries == answers.keys()
