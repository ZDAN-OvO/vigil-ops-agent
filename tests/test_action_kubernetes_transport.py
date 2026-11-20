"""用本地受控 HTTP 服务验证请求次数、补丁类型和 ownerReference。"""

import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace

import jsonpatch
import pytest
from actions_support import pod_plan, rollout_plan
from kubernetes import client

from aiops.actions import kubernetes_write
from aiops.actions.restart_pod import PodRestartAdapter
from aiops.actions.rollout_undo import CHANGE_CAUSE, REVISION_ANNOTATION, RolloutUndoAdapter


@pytest.mark.parametrize("response", ["redirect", "disconnect"])
@pytest.mark.parametrize("kind", ["rollback", "delete"])
def test_real_write_uses_one_http_request(monkeypatch, response, kind):
    requests = []

    class KubernetesStandIn(BaseHTTPRequestHandler):
        def do_PATCH(self):  # noqa: N802 - http.server 接口
            self.capture()

        def do_DELETE(self):  # noqa: N802 - http.server 接口
            self.capture()

        def capture(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(
                (self.command, self.path, self.headers["Content-Type"], json.loads(body))
            )
            if self.path.endswith("?dryRun=All"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    b'{"metadata":{},"spec":{"selector":{},"template":{"spec":{"containers":[]}}}}'
                )
            elif response == "redirect":
                self.send_response(307)
                self.send_header("Location", "/unexpected")
                self.end_headers()
            else:
                self.connection.shutdown(socket.SHUT_RDWR)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), KubernetesStandIn) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:

            def load_local(*, client_configuration):
                client_configuration.host = f"http://127.0.0.1:{server.server_port}"
                # 如果没有显式覆盖 HTTP 参数，重定向及 DELETE 断连都可能重发。
                client_configuration.retries = 2

            monkeypatch.setattr(kubernetes_write.config, "load_incluster_config", load_local)
            if kind == "rollback":
                adapter = RolloutUndoAdapter()
                plan = rollout_plan()
                adapter.apply(plan, dry_run=True, proposal_id="act-test")
                with pytest.raises(Exception, match="307|RemoteDisconnected|closed"):
                    adapter.apply(plan, dry_run=False, proposal_id="act-test")
                dry_run, actual = requests
                assert dry_run[1] == f"{actual[1]}?dryRun=All"
                assert dry_run[2:] == actual[2:]
                assert actual[2] == "application/json-patch+json"
                deployment = {
                    "metadata": {"uid": "uid-1", "resourceVersion": "10", "annotations": {}},
                    "spec": {
                        "replicas": 3,
                        "template": {
                            "spec": {"containers": [{"name": "server", "env": [{"name": "OLD"}]}]},
                        },
                    },
                }
                patched = jsonpatch.apply_patch(deployment, actual[3])
                assert patched["spec"] == {"template": plan.template, "replicas": 3}
                assert patched["metadata"]["annotations"][CHANGE_CAUSE] == "vigil rollback act-test"
                deployment["metadata"]["resourceVersion"] = "11"
                with pytest.raises(jsonpatch.JsonPatchTestFailed):
                    jsonpatch.apply_patch(deployment, actual[3])
            else:
                adapter = PodRestartAdapter()
                with pytest.raises(Exception, match="307|RemoteDisconnected|closed"):
                    adapter.execute(pod_plan())
                (actual,) = requests
                assert actual[0] == "DELETE"
                assert actual[3]["preconditions"] == {"uid": "pod-uid-1"}
            assert all(path != "/unexpected" for _, path, _, _ in requests)
        finally:
            server.shutdown()
            thread.join()


def test_rollback_chooses_only_the_expected_deployment_replicaset(monkeypatch):
    adapter = RolloutUndoAdapter()
    adapter._client = client.ApiClient()

    def template(image):
        return client.V1PodTemplateSpec(
            metadata=client.V1ObjectMeta(labels={"app": "shared"}),
            spec=client.V1PodSpec(containers=[client.V1Container(name="server", image=image)]),
        )

    deployment = SimpleNamespace(
        metadata=client.V1ObjectMeta(
            uid="dep-uid",
            resource_version="88",
            annotations={
                REVISION_ANNOTATION: "3",
            },
        ),
        spec=SimpleNamespace(
            template=template("v3"), selector=SimpleNamespace(match_labels={}), replicas=1
        ),
    )

    def rs(name, replicaset_uid, image, revision, **owner_overrides):
        owner = {
            "api_version": "apps/v1",
            "kind": "Deployment",
            "uid": "dep-uid",
            "controller": True,
            "name": "catalog",
        } | owner_overrides
        return SimpleNamespace(
            metadata=client.V1ObjectMeta(
                name=name,
                uid=replicaset_uid,
                resource_version="11",
                annotations={REVISION_ANNOTATION: str(revision)},
                owner_references=[client.V1OwnerReference(**owner)],
            ),
            spec=SimpleNamespace(template=template(image)),
        )

    replicasets = [
        rs("target", "target-uid", "v1", 1),
        rs("different-owner", "foreign-uid", "foreign", 2, uid="foreign-deployment"),
        rs("uncontrolled", "uncontrolled-uid", "foreign", 2, controller=False),
    ]
    apps = SimpleNamespace(
        read_namespaced_deployment=lambda *args, **kwargs: deployment,
        list_namespaced_replica_set=lambda *args, **kwargs: SimpleNamespace(items=replicasets),
    )
    monkeypatch.setattr(adapter, "_api", lambda: apps)
    plan = adapter.plan("vigil-shop", "catalog")
    assert plan.replica_set_uid == "target-uid"
    assert plan.target_image == "v1"
    assert plan.changes == [
        {"op": "replace", "path": "/spec/containers/0/image", "current": "v3", "value": "v1"}
    ]
