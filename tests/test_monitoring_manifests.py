"""核对实验清单与固定查询及页面跳转契约。"""

import json
from pathlib import Path

import yaml

from aiops.monitoring.snapshot import DASHBOARDS

MANIFESTS = Path(__file__).resolve().parents[1] / "env" / "manifests"


def _objects(name):
    return {
        (obj["kind"], obj["metadata"]["name"]): obj
        for obj in yaml.safe_load_all((MANIFESTS / name).read_text())
    }


def test_existing_frontend_probe_and_new_entry_probes_are_distinct():
    config = _objects("monitoring.yaml")[("ConfigMap", "prometheus-config")]["data"]
    prometheus = yaml.safe_load(config["prometheus.yml"])
    jobs = {job["job_name"]: job for job in prometheus["scrape_configs"]}
    legacy = jobs["frontend-probe"]
    assert legacy["static_configs"] == [
        {"targets": ["http://frontend.vigil-shop.svc.cluster.local./"]}
    ]
    assert legacy["params"] == {"module": ["http_2xx"]}
    (rule,) = yaml.safe_load(config["rules.yml"])["groups"][0]["rules"]
    assert rule["alert"] == "FrontendProbeFailed" and rule["for"] == "1m"
    assert rule["expr"] == (
        'probe_success{job="frontend-probe",'
        'instance="http://frontend.vigil-shop.svc.cluster.local./"} == 0'
    )
    entry = jobs["vigil-entry-probe"]
    (group,) = entry["static_configs"]
    assert group["labels"] == {"application": "online-boutique"}
    assert len(group["targets"]) == 3
    assert any(item["target_label"] == "address" for item in entry["relabel_configs"])


def test_cadvisor_collects_only_experiment_container_signals():
    manifests = _objects("monitoring.yaml")
    rules = manifests[("ClusterRole", "vigil-prometheus-cadvisor")]["rules"]
    assert rules == [
        {"apiGroups": [""], "resources": ["nodes"], "verbs": ["list", "watch"]},
        {"apiGroups": [""], "resources": ["nodes/metrics"], "verbs": ["get"]},
    ]
    config = manifests[("ConfigMap", "prometheus-config")]["data"]
    jobs = {
        job["job_name"]: job for job in yaml.safe_load(config["prometheus.yml"])["scrape_configs"]
    }
    assert jobs["kubelet-cadvisor"]["scheme"] == "https"
    assert "insecure_skip_verify" not in jobs["kubelet-cadvisor"]["tls_config"]
    assert jobs["kubelet-cadvisor"]["metrics_path"] == "/metrics/cadvisor"


def test_grafana_provisions_five_dashboard_uids_and_read_only_sources():
    manifests = _objects("grafana.yaml")
    cfg = manifests[("ConfigMap", "vigil-grafana-provisioning")]["data"]
    sources = yaml.safe_load(cfg["datasources.yaml"])["datasources"]
    assert {source["type"] for source in sources} == {"prometheus", "alertmanager"}
    assert all(
        not source["editable"] and source["url"].startswith("http://127.0.0.1:")
        for source in sources
    )
    assert cfg["query-proxy.conf"].count("limit_except GET { deny all; }") == 2
    assert cfg["query-proxy.conf"].count("proxy_pass ") == 2
    config_data = manifests[("ConfigMap", "vigil-grafana-dashboards")]["data"]
    dashboards = {parsed["uid"]: parsed for parsed in map(json.loads, config_data.values())}
    assert set(dashboards) == set(DASHBOARDS.values())
    for dashboard in dashboards.values():
        assert dashboard["editable"] is False
        assert all(
            panel["datasource"]["uid"] == "vigil-prometheus" for panel in dashboard["panels"]
        )
    for name in ("services", "nodes"):
        expressions = " ".join(
            target["expr"]
            for panel in dashboards[DASHBOARDS[name]]["panels"]
            for target in panel["targets"]
        )
        assert "container_cpu_usage_seconds_total" in expressions
        assert "container_memory_working_set_bytes" in expressions
        assert "or vector(0)" not in expressions
    release = dashboards[DASHBOARDS["changes"]]["panels"][0]
    assert release["targets"][0]["instant"]
    assert "vigil_release_timestamp_seconds" in release["targets"][0]["expr"]
