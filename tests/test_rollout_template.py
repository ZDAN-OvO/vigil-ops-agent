"""通过真实本机 HTTP 检查 Kubernetes 原始回滚请求。"""

import json
import threading
from contextlib import contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from actions_support import make_proposal
from urllib3.exceptions import ReadTimeoutError

from aiops.actions.rollout_undo import REVISION_ANNOTATION, ActionError, RolloutUndoAdapter


def resources():
    deployment = {
        "metadata": {
            "uid": "deployment-id",
            "resourceVersion": "12",
            "annotations": {REVISION_ANNOTATION: "4", "retained": "true"},
        },
        "spec": {
            "replicas": 2,
            "selector": {"matchLabels": {"app": "demo"}},
            "template": {
                "metadata": {"labels": {"app": "demo"}, "finalizers": ["demo/retain"]},
                "spec": {
                    "containers": [
                        {
                            "name": "server",
                            "image": "new",
                            "resources": {"limits": {"memory": "16Mi"}},
                            "futureField": {"value": "unknown-to-sdk"},
                        }
                    ]
                },
            },
        },
    }
    previous = deepcopy(deployment["spec"]["template"])
    previous["metadata"] = {"labels": {"app": "demo", "pod-template-hash": "history"}}
    container = previous["spec"]["containers"][0]
    container.pop("futureField")
    container.update(image="old", resources={"limits": {"memory": "128Mi"}})
    history = {
        "metadata": {
            "name": "demo-history",
            "uid": "history-id",
            "annotations": {REVISION_ANNOTATION: "3"},
            "ownerReferences": [{"kind": "Deployment", "uid": "deployment-id", "controller": True}],
        },
        "spec": {"template": previous},
    }
    return deployment, history


@contextmanager
def local_api(*, code=200, timeout=False, hang_get=False):
    deployment, history = resources()
    calls = []
    finish = threading.Event()

    class Api(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, value):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if hang_get:
                finish.wait(5)
                return
            if self.path.endswith("/replicasets/demo-history"):
                self.reply(200, history)
            elif "/replicasets" in self.path:
                self.reply(200, {"items": [history]})
            else:
                self.reply(200, deployment)

        def do_PUT(self):
            value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, value))
            if "dryRun=All" in self.path:
                self.reply(200, value)
                return
            if timeout:
                finish.wait(2)
                return
            if code == 200:
                deployment.update(value)
            self.reply(code, {"status": str(code)})

    with ThreadingHTTPServer(("127.0.0.1", 0), Api) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            yield server.server_port, calls, deployment, history
        finally:
            finish.set()
            server.shutdown()
            thread.join()


def adapter_for(port, monkeypatch):
    def load(*args, client_configuration, **kwargs):
        client_configuration.host = f"http://127.0.0.1:{port}"
        client_configuration.proxy = None

    monkeypatch.setattr("aiops.actions.rollout_undo.k8s_config.load_incluster_config", load)
    return RolloutUndoAdapter(timeout_seconds=0.25)


def test_raw_template_diff_and_replace_preserve_other_fields(monkeypatch):
    with local_api() as (port, calls, deployment, _):
        adapter = adapter_for(port, monkeypatch)
        before = deepcopy(deployment)
        plan = adapter.plan("vigil-shop", "demo")
        assert plan.target_revision == 3
        assert plan.replicaset_uid == "history-id"
        changes = {item["path"]: (item["from"], item["to"]) for item in plan.changes}
        assert changes == {
            "metadata.finalizers": (["demo/retain"], None),
            "spec.containers[server].futureField": ({"value": "unknown-to-sdk"}, None),
            "spec.containers[server].image": ("new", "old"),
            "spec.containers[server].resources.limits.memory": ("16Mi", "128Mi"),
        }
        adapter.apply(plan, dry_run=True, proposal_id="test")
        adapter.apply(plan, dry_run=False, proposal_id="test")
        before["spec"]["template"] = plan.template
        assert len(calls) == 2
        assert calls[0][1] == calls[1][1] == before == deployment
        assert adapter.template_matches(plan)


@pytest.mark.parametrize("changed_field", ["uid", "controller", "template"])
def test_raw_recheck_rejects_replaced_or_changed_history(monkeypatch, changed_field):
    with local_api() as (port, calls, _, history):
        adapter = adapter_for(port, monkeypatch)
        plan = adapter.plan("vigil-shop", "demo")
        initial = make_proposal()
        proposal = initial.model_copy(
            update={
                "parameters": plan.pinned_parameters,
                "target": initial.target.model_copy(
                    update={
                        "uid": plan.deployment_uid,
                        "name": plan.name,
                        "resource_version": plan.resource_version,
                    }
                ),
            }
        )
        if changed_field == "uid":
            history["metadata"]["uid"] = "other"
        if changed_field == "controller":
            history["metadata"]["ownerReferences"][0]["uid"] = "other"
        if changed_field == "template":
            history["spec"]["template"]["spec"]["containers"][0]["image"] = "other"
        with pytest.raises(ActionError, match="历史 ReplicaSet") as caught:
            adapter.recheck(proposal)
        assert caught.value.code == "revision_changed"
        assert calls == []


def test_transport_timeout_emits_exactly_one_put(monkeypatch):
    with local_api(timeout=True) as (port, calls, _, _):
        adapter = adapter_for(port, monkeypatch)
        plan = adapter.plan("vigil-shop", "demo")
        with pytest.raises((TimeoutError, ReadTimeoutError)):
            adapter.apply(plan, dry_run=False, proposal_id="test")
        assert len(calls) == 1


def test_rollout_poll_does_not_hang_on_a_stalled_api(monkeypatch):
    """写后轮询遇到不返回的 API 时按时限抛错，交给执行器按"已写入、待核实"收尾。"""
    import time

    with local_api() as (port, _, _, _):
        adapter = adapter_for(port, monkeypatch)
        plan = adapter.plan("vigil-shop", "demo")
    with local_api(hang_get=True) as (port, _, _, _):
        adapter = adapter_for(port, monkeypatch)
        started = time.monotonic()
        with pytest.raises((TimeoutError, ReadTimeoutError)):
            adapter.wait_rolled_out(plan, timeout_seconds=1)
        with pytest.raises((TimeoutError, ReadTimeoutError)):
            adapter.state("vigil-shop", "demo")
        assert time.monotonic() - started < 4
