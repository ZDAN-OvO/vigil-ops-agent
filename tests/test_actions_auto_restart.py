"""旧 Pod 删除的入口拒绝、历史停止信号和适配器前置条件。"""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from actions_support import (
    DSN,
    TEST_REASON,
    FakeAdapter,
    FakePrometheus,
    FakeRestartAdapter,
    build_executor,
    make_proposal,
    pod_plan,
    rollout_plan,
)
from controlled_action_support import (
    REPLICA_NAMESPACE,
    REPLICA_TARGET,
    drive_replica,
    replica_runtime,
)

from aiops.actions.executor import ActionExecutor
from aiops.actions.remediation_alerts import link_firing, record_firing
from aiops.actions.restart_pod import PodRestartAdapter
from aiops.contracts import (
    ActionOrigin,
    ActionTarget,
    AttemptStatus,
    ProposalStatus,
    RiskDecision,
    RiskDisposition,
)
from aiops.db import Store

pytestmark = pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL，跳过真实库测试")


@pytest.mark.parametrize("namespace", ["vigil-experiment", "vigil-shop"])
@pytest.mark.parametrize("origin", [ActionOrigin.OPERATOR, ActionOrigin.MODEL])
def test_restart_pod_is_denied_without_approval_or_write(tmp_path, cleanup, namespace, origin):
    async def run():
        pod = pod_plan(namespace=namespace)
        adapter = FakeRestartAdapter(pod)
        async with Store.open(DSN) as store:
            await store.migrate()
            executor = await build_executor(
                store,
                FakeAdapter(rollout_plan()),
                FakePrometheus([200.0]),
                tmp_path,
                cleanup=cleanup,
                restart_adapter=adapter,
            )
            proposal, risk = await executor.propose_restart_pod(
                pod=pod.name,
                service="rebuild-demo",
                path="/work",
                reason=TEST_REASON,
                actor="tester",
                namespace=namespace,
                origin=origin,
                evidence_ids=["ev-1"] if origin is ActionOrigin.MODEL else [],
            )
            cleanup.append(proposal.id)
            assert risk.disposition is RiskDisposition.DENY
            assert (await store.get_proposal(proposal.id)).status is ProposalStatus.DENIED
            assert await store.get_approval(proposal.id) is None
            assert await store.get_attempt(proposal.id) is None
            assert (
                await executor._safety.get_card(proposal.id, session_id=proposal.session_id) is None
            )
            assert not any(
                event.event in {"action.auto_approved", "action.send_granted"}
                for event in await store.list_audit(proposal.id)
            )
            assert adapter.calls == []
            assert adapter.pod_get_calls == 0

    asyncio.run(run())


@pytest.mark.parametrize(
    "status",
    [
        ProposalStatus.APPROVED,
        ProposalStatus.EXECUTING,
        ProposalStatus.FAILED,
        ProposalStatus.UNKNOWN,
    ],
)
def test_history_signal_uses_workload_uid_and_cluster(tmp_path, cleanup, status):
    """旧 Deployment 回退没有 workload 字段，历史查询仍能按目标 UID 找到。"""

    async def run():
        async with Store.open(DSN) as store:
            await store.migrate()
            suffix = uuid4().hex
            target = ActionTarget(
                cluster="vigil-lab",
                namespace="vigil-experiment",
                kind="Deployment",
                name="rebuild-demo",
                uid=f"deployment-{suffix}",
                resource_version="1",
            )
            old = make_proposal(
                id=f"act-{suffix}",
                idempotency_key=suffix,
                target=target,
                parameters={},
                status=status,
                created_at=datetime.now(UTC),
            )
            cleanup.append(old.id)
            risk = RiskDecision(
                proposal_id=old.id,
                disposition=RiskDisposition.APPROVAL,
                policy_version="legacy",
                reasons=["历史提案"],
                requires_compensation=False,
            )
            await store.save_proposal(old, risk)
            for later_status in (
                ProposalStatus.PENDING_APPROVAL,
                ProposalStatus.REJECTED,
                ProposalStatus.VOIDED,
            ):
                later = old.model_copy(
                    update={
                        "id": f"act-{uuid4().hex}",
                        "idempotency_key": uuid4().hex,
                        "status": later_status,
                        "created_at": datetime.now(UTC),
                    }
                )
                cleanup.append(later.id)
                await store.save_proposal(later, risk.model_copy(update={"proposal_id": later.id}))
            assert await store.latest_proposal_blocks_auto(
                target.cluster, target.namespace, target.uid, "changed-replica-set"
            )
            assert not await store.latest_proposal_blocks_auto(
                "another-cluster", target.namespace, target.uid
            )
            assert not await store.latest_proposal_blocks_auto(
                target.cluster, target.namespace, "another-workload"
            )

    asyncio.run(run())


def test_audit_failure_does_not_hide_the_denied_proposal(tmp_path, cleanup, monkeypatch):
    async def broken_audit(*args, **kwargs):
        raise ConnectionError("审计写入失败")

    monkeypatch.setattr(ActionExecutor, "_audit", broken_audit)

    async def run():
        adapter = FakeRestartAdapter(pod_plan(namespace="vigil-experiment"))
        async with Store.open(DSN) as store:
            await store.migrate()
            executor = await build_executor(
                store,
                FakeAdapter(rollout_plan()),
                FakePrometheus([200.0]),
                tmp_path,
                cleanup=cleanup,
                restart_adapter=adapter,
            )
            proposal, risk = await executor.propose_restart_pod(
                pod="rebuild-demo-abc",
                service="rebuild-demo",
                path="/work",
                reason=TEST_REASON,
                actor="tester",
                namespace="vigil-experiment",
            )
            cleanup.append(proposal.id)
            assert risk.disposition is RiskDisposition.DENY
            assert (await store.get_proposal(proposal.id)).status is ProposalStatus.DENIED
            assert await store.get_approval(proposal.id) is None
            assert await store.get_attempt(proposal.id) is None
            assert adapter.calls == []

    asyncio.run(run())


def test_new_rule_sends_once_after_approval_free_evaluation(isolated_serve_dsn):
    async def firing(runtime, fingerprint):
        group = "replica-capacity"
        sessions = runtime.ledger.sessions
        async with sessions.alert_transaction(group) as conn:
            receipt = await sessions.record_alert(
                conn,
                group,
                title="容量不足",
                text="容量不足",
                display_text="容量不足",
                repeat_display_text="再次告警",
            )
            identity = {
                "cluster": runtime.settings.cluster,
                "group_key": group,
                "fingerprint": fingerprint,
                "starts_at": datetime.now(UTC),
            }
            assert await record_firing(
                conn,
                **identity,
                labels={
                    "alertname": "VigilReplicaCapacityDegraded",
                    "source": "capacity",
                    "namespace": REPLICA_NAMESPACE,
                    "target": REPLICA_TARGET,
                },
            )
            await link_firing(conn, **identity, session_id=receipt.session_id, evaluate=True)

    async def run():
        async with Store.open(isolated_serve_dsn) as store:
            await store.migrate()
            runtime = replica_runtime(store, after=0.0)
            runtime.settings = runtime.settings.model_copy(
                update={"remediation_auto_enabled": True}
            )
            await firing(runtime, "first")
            await runtime.evaluate_one()
            (proposal_id,) = await runtime.ledger.due()
            approval = await store.get_approval(proposal_id)
            assert approval.actor == f"policy:{runtime.policy.version}"
            first = await drive_replica(runtime, proposal_id)
            assert first.status is AttemptStatus.FAILED
            assert first.verification["outcome"] == "no_improvement"
            assert runtime.kube.writes == [{"replicas": 2}]
            await firing(runtime, "second")
            await runtime.evaluate_one()
            assert await runtime.ledger.due() == []
            await runtime.drive(proposal_id)
            assert runtime.kube.writes == [{"replicas": 2}]
            async with store.transaction() as conn:
                cursor = await conn.execute(
                    "SELECT count(*) AS total FROM remediation_jobs WHERE sent_at IS NOT NULL"
                )
                assert (await cursor.fetchone())["total"] == 1

    asyncio.run(run())


def test_delete_request_is_bound_to_the_approved_uid():
    options = PodRestartAdapter()._delete_options(pod_plan())

    assert options.preconditions.uid == "pod-uid-1"
    assert options.preconditions.resource_version is None


def test_wait_rebuilt_ignores_pods_that_existed_before_deletion(monkeypatch):
    adapter = PodRestartAdapter()
    plan = pod_plan(sibling_uids=("sibling-uid",))
    monkeypatch.setattr(
        adapter,
        "pods_of_owner",
        lambda namespace, owner_uid: [
            {"name": "old", "uid": "pod-uid-1", "ready": True, "restarts": 0},
            {"name": "sibling", "uid": "sibling-uid", "ready": True, "restarts": 0},
        ],
    )

    assert adapter.wait_rebuilt(plan, 0.3, sample_seconds=0.1) is None
