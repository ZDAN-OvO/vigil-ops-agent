"""通过真实动作运行时与 K8s 适配器验证已审批字段的写入边界。"""

import asyncio
import json
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

import jsonpatch
import pytest
from controlled_action_support import (
    REPLICA_NAMESPACE,
    REPLICA_TARGET,
    drive_replica,
    replica_runtime,
)
from kubernetes.client import ApiClient, Configuration
from replica_card_support import replica_card
from urllib3.response import HTTPResponse

from aiops.actions.policy import target_key
from aiops.actions.remediation_kube import RemediationKubeAdapter
from aiops.contracts import ApprovalDecision, AttemptStatus, ProposalStatus
from aiops.db import Store

_UID = "fixture-deployment"
_PATH = f"/apis/apps/v1/namespaces/{REPLICA_NAMESPACE}/deployments/{REPLICA_TARGET}"
_ADMISSION = "/apis/admissionregistration.k8s.io/v1"
_POLICY_NAME = "vigil-remediation-write"


def _healthy(replicas, generation):
    return {
        "observedGeneration": generation,
        "replicas": replicas,
        "updatedReplicas": replicas,
        "readyReplicas": replicas,
        "availableReplicas": replicas,
    }


def _patch(version):
    return [
        {"op": "test", "path": "/metadata/uid", "value": _UID},
        {"op": "test", "path": "/metadata/resourceVersion", "value": version},
        {"op": "test", "path": "/spec/replicas", "value": 1},
        {"op": "replace", "path": "/spec/replicas", "value": 2},
    ]


class _ClusterTransport:
    def __init__(self, *, drift_after_dry_run=False):
        self.deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "namespace": REPLICA_NAMESPACE,
                "name": REPLICA_TARGET,
                "uid": _UID,
                "resourceVersion": "100",
                "generation": 1,
            },
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "replica-fixture"}},
                "template": {
                    "metadata": {"labels": {"app": "replica-fixture"}},
                    "spec": {"containers": [{"name": "app", "image": "app:v1"}]},
                },
            },
            "status": _healthy(1, 1),
        }
        self.drift_after_dry_run = drift_after_dry_run
        self.patches = []
        self.policy_version = None
        self.read_client = ApiClient(Configuration(host="https://example.invalid"))
        self.write_client = ApiClient(Configuration(host="https://example.invalid"))
        for api in (self.read_client, self.write_client):
            api.rest_client.pool_manager.request = self.request
        self.adapter = RemediationKubeAdapter(
            read_client=self.read_client,
            write_client=self.write_client,
            is_registered=lambda target: (
                (
                    target.cluster,
                    target.namespace,
                    target.kind,
                    target.name,
                )
                == ("vigil-lab", REPLICA_NAMESPACE, "Deployment", REPLICA_TARGET)
            ),
        )

    def drift(self, *, replicas):
        metadata = self.deployment["metadata"]
        metadata["resourceVersion"] = "101"
        metadata["annotations"] = {"fixture.example/other-writer": "preserved"}
        self.deployment["spec"]["replicas"] = replicas

    @staticmethod
    def _response(payload, status=200):
        return HTTPResponse(
            body=json.dumps(payload).encode(),
            status=status,
            headers={"Content-Type": "application/json"},
        )

    def request(self, method, url, *, headers, body=None, **kwargs):
        route = urlsplit(url)
        if method == "GET":
            if route.path == _PATH:
                return self._response(self.deployment)
            if route.path == f"{_ADMISSION}/validatingadmissionpolicies/{_POLICY_NAME}":
                return self._response(
                    {
                        "metadata": {
                            "annotations": {"vigil.aiops/policy-version": self.policy_version},
                        },
                        "spec": {"failurePolicy": "Fail"},
                        "status": {"typeChecking": {}},
                    },
                )
            assert route.path == f"{_ADMISSION}/validatingadmissionpolicybindings/{_POLICY_NAME}"
            return self._response(
                {"spec": {"policyName": _POLICY_NAME, "validationActions": ["Deny"]}},
            )

        assert method == "PATCH" and route.path == _PATH
        assert headers["Content-Type"] == "application/json-patch+json"
        assert kwargs["retries"] is False and kwargs["redirect"] is False
        query = parse_qs(route.query, keep_blank_values=True)
        assert query in ({}, {"dryRun": ["All"]})
        operations = json.loads(body)
        dry_run = bool(query)
        self.patches.append((dry_run, operations))
        try:
            updated = jsonpatch.apply_patch(self.deployment, operations)
        except jsonpatch.JsonPatchException:
            return self._response({"message": "conflict"}, status=422)
        updated["metadata"]["generation"] += 1
        if dry_run:
            if self.drift_after_dry_run:
                self.drift(replicas=3)
        else:
            updated["metadata"]["resourceVersion"] = "102"
            updated["status"] = _healthy(updated["spec"]["replicas"], 2)
            self.deployment = updated
        return self._response(updated)


async def _approved(store, transport):
    await store.migrate()
    runtime = replica_runtime(store, kube=transport.adapter)
    transport.policy_version = runtime.policy.version
    _, proposal, card = await replica_card(runtime)
    await runtime.ledger.safety.record_card_decision(
        proposal.id,
        session_id=proposal.session_id,
        fingerprint=card.payload["fingerprint"],
        decision=ApprovalDecision.APPROVED,
        actor="tester",
    )
    return runtime, proposal


@pytest.mark.parametrize("drift_at", ["approval", "baseline", "dry_run"])
def test_scale_rechecks_replica_drift_before_write(isolated_serve_dsn, monkeypatch, drift_at):
    async def scenario():
        async with Store.open(isolated_serve_dsn) as store:
            transport = _ClusterTransport(drift_after_dry_run=drift_at == "dry_run")
            runtime, proposal = await _approved(store, transport)
            if drift_at == "approval":
                transport.drift(replicas=3)
            if drift_at == "baseline":
                samples = runtime.metrics.raw_samples

                async def drift_during_baseline(selector, start, end):
                    points = await samples(selector, start, end)
                    transport.drift(replicas=3)
                    return points

                monkeypatch.setattr(runtime.metrics, "raw_samples", drift_during_baseline)
            await runtime.drive(proposal.id)
            assert proposal.target.resource_version == "100"
            assert transport.deployment["metadata"]["uid"] == proposal.target.uid
            assert transport.deployment["metadata"]["resourceVersion"] == "101"
            assert transport.deployment["spec"]["replicas"] == 3
            assert transport.patches == ([(True, _patch("100"))] if drift_at == "dry_run" else [])
            assert await store.get_attempt(proposal.id) is None
            assert (await store.get_proposal(proposal.id)).status is ProposalStatus.FAILED
            assert (await store.get_approval(proposal.id)).revoked_at is not None
            target = proposal.target
            assert not await store.target_is_locked(
                target_key(target.cluster, target.namespace, target.kind, target.name)
            )
            assert not await runtime.ledger.due()
            events = await runtime.ledger.sessions.list_events(proposal.session_id)
            [result] = [event.payload for event in events if event.type == "action_result"]
            assert result["status"] == "failed" and result["written"] is False
            assert result["reason"] == "写入准备失败（drift）"

    asyncio.run(scenario())


def test_scale_allows_rv_drift_with_unchanged_replicas(isolated_serve_dsn):
    async def scenario():
        async with Store.open(isolated_serve_dsn) as store:
            transport = _ClusterTransport()
            runtime, proposal = await _approved(store, transport)
            transport.drift(replicas=1)
            expected_spec = deepcopy(transport.deployment["spec"])
            expected_spec["replicas"] = 2
            attempt = await drive_replica(runtime, proposal.id)
            assert proposal.target.resource_version == "100"
            assert transport.deployment["metadata"]["uid"] == proposal.target.uid
            assert transport.deployment["metadata"]["resourceVersion"] == "102"
            assert transport.deployment["metadata"]["annotations"] == {
                "fixture.example/other-writer": "preserved"
            }
            assert transport.deployment["spec"] == expected_spec
            assert transport.patches == [(True, _patch("101")), (False, _patch("101"))]
            assert attempt.status is AttemptStatus.VERIFIED
            assert attempt.result["written"] is True
            assert (await store.get_proposal(proposal.id)).status is ProposalStatus.SUCCEEDED

    asyncio.run(scenario())
