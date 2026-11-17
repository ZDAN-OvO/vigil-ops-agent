"""规则基线的判定链：每个签名都要能从真实形态的数据里判出来。"""

from datetime import UTC, datetime, timedelta

from aiops.contracts import TimeRange
from aiops.investigation.rules import Facts, Signature, decide

NOW = datetime.now(UTC)
WINDOW = TimeRange(start=NOW - timedelta(minutes=15), end=NOW)


def _container(*, started_at=None, restarts=0, limit="128Mi", last_reason=None, ready=True):
    return {
        "name": "server",
        "ready": ready,
        "restart_count": restarts,
        "started_at": started_at or NOW - timedelta(minutes=1),
        "last_terminated": {"reason": last_reason} if last_reason else None,
        "resources": {"limits": {"memory": limit}, "requests": {"memory": "64Mi"}},
    }


def _pod(name, containers, labels=None):
    return {"name": name, "labels": labels or {"app": "svc"}, "containers": containers}


def _deployment(name, revisions, image):
    return {
        "name": name,
        "uid": f"uid-{name}",
        "labels": {"app": name},
        "revision": str(revisions[-1][0]),
        "images": [image],
        "containers": [{"name": "server", "image": image, "limits": {}, "requests": {}}],
        "revisions": [
            {
                "revision": revision,
                "image": revision_image,
                "created_at": created_at,
                "ready_replicas": ready,
                "replicas": 0,
            }
            for revision, revision_image, created_at, ready in revisions
        ],
    }


def _facts(**overrides):
    base = {
        "service": "svc",
        "window": WINDOW,
        "deployments": [],
        "all_deployments": [],
        "all_services": [],
        "pods": [],
        "logs": [],
        "probe_success": 1.0,
        "probe_duration": 0.01,
    }
    base.update(overrides)
    return Facts(**base)


def test_release_regression_detects_image_change_and_entry_failure():
    old = "registry/productcatalog@sha256:" + "a" * 64
    new = "vigil/productcatalog:canary-20261104"
    old_created = NOW - timedelta(days=13)
    facts = _facts(
        service="productcatalogservice",
        deployments=[
            _deployment(
                "productcatalogservice",
                [(95, old, old_created, 0), (96, new, old_created, 1)],
                new,
            )
        ],
        pods=[_pod("productcatalogservice-1", [_container(started_at=NOW - timedelta(minutes=1))])],
        probe_success=0.0,
        probe_duration=0.4,
    )

    decision = decide(facts)

    assert decision.signature == Signature.RELEASE_REGRESSION
    assert decision.action == "rollout_undo"
    assert decision.action_target == "productcatalogservice"
    assert decision.confirmed is True


def test_same_image_resource_change_is_not_a_release_regression():
    image = "registry/currency@sha256:" + "b" * 64
    facts = _facts(
        service="currencyservice",
        deployments=[
            _deployment(
                "currencyservice",
                [
                    (52, image, NOW - timedelta(days=13), 0),
                    (53, image, NOW - timedelta(days=13), 1),
                ],
                image,
            )
        ],
        pods=[
            _pod(
                "currencyservice-1",
                [_container(restarts=5, limit="16Mi", last_reason="OOMKilled")],
            )
        ],
        probe_success=0.0,
    )

    decision = decide(facts)

    assert decision.signature == Signature.SPEC_SHORTAGE
    assert decision.action == ""
    assert decision.needs_human


def test_dependency_latency_names_the_redirected_service():
    deployment = _deployment("cartservice", [(1, "reg/cart@sha256:" + "c" * 64, NOW, 1)], "c")
    proxy = _deployment("redis-access-proxy", [(1, "proxy", NOW, 1)], "proxy")
    facts = _facts(
        service="cartservice",
        deployments=[deployment],
        all_deployments=[
            deployment,
            _deployment("redis-cart", [(1, "redis", NOW, 1)], "redis"),
            proxy,
        ],
        all_services=[
            {"name": "cartservice", "selector": {"app": "cartservice"}},
            {"name": "redis-cart", "selector": {"app": "redis-access-proxy"}},
        ],
        pods=[_pod("cartservice-1", [_container()])],
        probe_success=1.0,
        probe_duration=0.34,
    )

    decision = decide(facts)

    assert decision.signature == Signature.DEPENDENCY_LATENCY
    assert decision.action == ""
    assert "redis-cart" in decision.conclusion
    assert "redis-access-proxy" in decision.conclusion


def test_pod_local_state_needs_log_5xx_and_healthy_entry():
    image = "vigil/pod-rebuild-demo:v06"
    facts = _facts(
        service="rebuild-demo",
        deployments=[
            _deployment("rebuild-demo", [(1, image, NOW - timedelta(minutes=2), 1)], image)
        ],
        pods=[_pod("rebuild-demo-1", [_container()])],
        logs=[{"pod": "rebuild-demo-1", "lines": ['"GET /work HTTP/1.1" 500 24', "other"]}],
        probe_success=1.0,
        probe_duration=0.01,
    )

    decision = decide(facts)

    assert decision.signature == Signature.POD_LOCAL_STATE
    assert decision.action == "restart_pod"
    assert decision.action_target == "rebuild-demo-1"


def test_healthy_looking_data_returns_unknown_without_guessing():
    facts = _facts(
        pods=[_pod("svc-1", [_container()])], logs=[{"lines": ['"GET / HTTP/1.1" 200 12']}]
    )

    decision = decide(facts)

    assert decision.signature == Signature.UNKNOWN
    assert decision.confirmed is False
    assert decision.action == ""
