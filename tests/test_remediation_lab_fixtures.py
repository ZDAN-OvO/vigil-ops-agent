"""夹具在本机的 HTTP、配置和审计回归。"""

import importlib.util
import json
import socket
import tempfile
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_config_http_compensation_has_five_real_samples_per_window(monkeypatch):
    app = load_module("config_fixture", "faults/f05-service-config/app.py")
    monkeypatch.setenv("VIGIL_PROFILE", "compensate")
    monkeypatch.setenv("VIGIL_MODE", "healthy")
    fake_clock = type("Clock", (), {"time": lambda self: self.seconds})()
    monkeypatch.setattr(app, "time", fake_clock)
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    with tempfile.TemporaryDirectory(dir=ROOT) as directory, serving(server) as base:
        mode_file = Path(directory) / "mode"
        monkeypatch.setattr(app, "MODE_FILE", mode_file)
        mode_file.write_text("degraded", encoding="utf-8")
        for minute in range(5):
            fake_clock.seconds = (13 + minute) * 60 + 17
            assert status(base + "/configuration?source=configmap") == 503
            assert status(base + "/business?source=configmap") == (
                503 if (13 + minute) % 5 == 0 else 200
            )
            assert status(base + "/business?source=env") == 200
        mode_file.write_text("worsen", encoding="utf-8")
        for minute in range(5):
            fake_clock.seconds = (18 + minute) * 60 + 17
            assert status(base + "/business?source=configmap") == 503
        with urllib.request.urlopen(base + "/metrics", timeout=3) as response:
            metrics = response.read().decode()
            assert 'vigil_config_fixture_loaded_mode{source="configmap",mode="worsen"} 1' in metrics


def test_replica_gateway_submits_two_real_concurrent_transactions(monkeypatch):
    app = load_module("replica_fixture", "faults/f04-replica-capacity/app.py")
    resolver = socket.getaddrinfo
    workers = []

    def resolve(host, *args, **kwargs):
        if host == app.BACKENDS:
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", item) for item in workers]
        return resolver(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    first = app.Server(("127.0.0.1", 0), gateway=False)
    second = app.Server(("127.0.0.1", 0), gateway=False)
    gateway = app.Server(("127.0.0.1", 0), gateway=True)
    with serving(first), serving(second), serving(gateway) as base:
        workers.append(first.server_address)
        assert status(base + "/business") == 503
        assert "vigil_replica_fixture_endpoints 1" in app.metrics()
        assert "vigil_replica_fixture_rejected 1" in app.metrics()
        workers.append(second.server_address)
        assert status(base + "/business") == 200
        assert "vigil_replica_fixture_endpoints 2" in app.metrics()
        assert "vigil_replica_fixture_rejected 0" in app.metrics()


@contextmanager
def serving(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def status(url):
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def test_audit_collection_keeps_failed_and_late_terminal_requests():
    audit = load_module("audit_collector", "scripts/collect_remediation_audit.py")
    actor = audit.REMEDIATION_ACTOR
    since = audit.utc_time("2026-11-28T00:00:00+09:00")
    until = audit.utc_time("2026-11-27T15:01:00Z")
    started_at = "2026-11-27T15:00:52.000000Z"

    def event(audit_id, stage, code=None, query="", started=started_at, username=actor):
        return json.dumps(
            {
                "auditID": audit_id,
                "user": {"username": username},
                "verb": "patch",
                "requestURI": f"/api/v1/namespaces/test/configmaps/example{query}",
                "requestReceivedTimestamp": started,
                "stageTimestamp": "2026-11-27T15:01:02.000000Z" if code else started,
                "stage": stage,
                "responseStatus": {"code": code},
                "objectRef": {"resource": "configmaps", "name": "example", "namespace": "test"},
            }
        )

    start = "RequestReceived"
    end = "ResponseComplete"
    records = [
        event("one", start, query="?dryRun=%41ll"),
        event("one", end, code=200, query="?dryRun=%41ll"),
        event("one", end, code=200, query="?dryRun=%41ll"),
        event("two", start),
        event("two", end, code=403),
        event("three", end, code=504),
        event("four", start),
        event("five", end, code=200, username="operator"),
        event("six", start, started="2026-11-27T15:01:00Z"),
    ]
    requests = {
        item["audit_id"]: item for item in audit.collect(records, since, until, {actor, "operator"})
    }
    assert set(requests) == {"one", "two", "three", "four", "five"}
    assert requests["one"]["dry_run"]
    assert requests["one"]["outcome"] == "success"
    assert len(requests["one"]["stages"]) == 2
    assert requests["two"]["outcome"] == "rejected"
    assert requests["three"]["outcome"] == "timeout"
    assert requests["four"]["outcome"] == "incomplete"
    assert requests["five"]["actor"] == "operator"


def test_experiment_manifests_and_embedded_monitoring_config_parse():
    files = (
        "env/manifests/audit-policy.yaml",
        "env/manifests/monitoring.yaml",
        "faults/f02-memory-limit-oom/cpu-load.yaml",
        "faults/f04-replica-capacity/manifests.yaml",
        "faults/f05-service-config/manifests.yaml",
        "faults/f05-service-config/shared-consumer.yaml",
    )
    for file in files:
        for item in yaml.safe_load_all((ROOT / file).read_text(encoding="utf-8")):
            assert item["apiVersion"]
            assert item["kind"]
            if item["kind"] == "ConfigMap":
                for name, value in item["data"].items():
                    if name.endswith(".yml"):
                        assert yaml.safe_load(value)
    items = yaml.safe_load_all((ROOT / "env/manifests/monitoring.yaml").read_text(encoding="utf-8"))
    configs = {
        entry["metadata"]["name"]: entry["data"] for entry in items if entry["kind"] == "ConfigMap"
    }
    prometheus = yaml.safe_load(configs["prometheus-config"]["prometheus.yml"])
    jobs = [entry["job_name"] for entry in prometheus["scrape_configs"]]
    assert len(jobs) == len(set(jobs))
    assert {"vigil-config-business", "vigil-config-probe", "vigil-replica-probe"} <= set(jobs)
    alertmanager = yaml.safe_load(configs["alertmanager-config"]["alertmanager.yml"])
    assert alertmanager["route"]["group_by"] == ["alertname", "namespace", "target", "source"]
    assert alertmanager["receivers"][1]["webhook_configs"][0]["send_resolved"]
