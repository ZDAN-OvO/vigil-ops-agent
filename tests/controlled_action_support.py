"""受控副本调整测试共用的持久账本、集群和指标替身。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from aiops.actions.policy import load_policy
from aiops.actions.remediation_kube import PatchPlan, Snapshot, Verification, WriteReceipt
from aiops.actions.remediation_runtime import RemediationRuntime
from aiops.actions.remediation_store import RemediationStore
from aiops.actions.safety import ActionSafety
from aiops.config import PROJECT_ROOT, Settings
from aiops.contracts import ActionAttempt, ActionKind, ActionOrigin, ActionTarget, AttemptStatus

REPLICA_TARGET = "vigil-replica-fixture"
REPLICA_NAMESPACE = "vigil-experiment"


class ReplicaAdapter:
    def __init__(self, *, record_write=None):
        self._target = ActionTarget(
            cluster="vigil-lab",
            namespace=REPLICA_NAMESPACE,
            kind="Deployment",
            name=REPLICA_TARGET,
            uid="fixture-deployment",
            resource_version="1",
        )
        self.record_write = record_write
        self.dry_runs = []
        self.writes = []

    def target(self, cluster, namespace, kind, name):
        assert (cluster, namespace, kind, name) == (
            self._target.cluster,
            self._target.namespace,
            self._target.kind,
            self._target.name,
        )
        return self._target

    def snapshot(self, target, kind, params, *, config_map_target=None):
        assert kind == ActionKind.SCALE_REPLICAS and config_map_target is None
        return Snapshot(target, target, kind, params, {"replicas": 1}, {"replicas": 1})

    def current_parameters(self, snapshot):
        return snapshot.current

    def require_admission(self, policy_version, *, exclusive_config):
        assert policy_version
        assert exclusive_config is False

    def prepare(self, snapshot):
        return PatchPlan(snapshot, {"replicas": snapshot.parameters["replicas"]}, "[]", "")

    def dry_run(self, plan):
        self.dry_runs.append(plan.after)
        return plan

    def send(self, plan, *, authorize):
        authorize()
        self.writes.append(plan.after)
        if self.record_write:
            self.record_write(plan.after)
        return WriteReceipt(plan.before.object.model_copy(update={"resource_version": "2"}), 2)

    def verify(self, plan, receipt):
        assert receipt.object.uid == plan.before.object.uid
        return Verification("ready")


class ProbeSamples:
    def __init__(self, *, after=1.0, before=0.0):
        self.after = after
        self.before = before

    async def raw_samples(self, selector, start, end):
        duration = int((end - start).total_seconds())
        if not selector.startswith("probe_success"):
            timestamps = [start.timestamp() + offset for offset in range(0, duration, 15)]
            if selector.startswith("vigil_replica_fixture_batch_timestamp_seconds"):
                return [(at, at) for at in timestamps]
            if selector.startswith("vigil_replica_fixture_rejected"):
                return [(at, 0.0) for at in timestamps]
            assert selector.startswith("vigil_replica_fixture_endpoints")
            return [(at, 2.0) for at in timestamps]
        value = self.before if duration == 300 else self.after
        if value is None:
            return []
        return [(start.timestamp() + offset, value) for offset in range(0, duration, 60)]

    async def fresh(self, selector, at, *, max_age):
        assert max_age > 0
        if selector.startswith("probe_success"):
            return self.before, at.timestamp()
        assert selector.startswith(
            ("vigil_replica_fixture_rejected", "vigil_replica_fixture_endpoints")
        )
        return 1, at.timestamp()


def replica_runtime(store, *, kube=None, after=1.0, approval_ttl_seconds=900):
    settings = Settings(_env_file=None, model_name="fixture")
    safety = ActionSafety(store, approval_ttl_seconds=approval_ttl_seconds)
    policy = load_policy(PROJECT_ROOT / "config/remediation")
    assert policy.error is None
    return RemediationRuntime(
        settings,
        RemediationStore(store, safety),
        policy,
        kube or ReplicaAdapter(),
        ProbeSamples(after=after),
    )


async def prepare_replica(executor, runtime, *, replicas=2, reason="扩展业务容量"):
    lease, turn_id = executor._scope()
    proposal, _, _ = await runtime.prepare_manual(
        lease=lease,
        turn_id=turn_id,
        kind=ActionKind.SCALE_REPLICAS,
        target_kind="Deployment",
        namespace=REPLICA_NAMESPACE,
        name=REPLICA_TARGET,
        parameters={"replicas": replicas},
        investigation_id=None,
        evidence_ids=["fixture-observation"],
        reason=reason,
        origin=ActionOrigin.OPERATOR,
    )
    return proposal


async def finish_observation(runtime, proposal_id):
    """推进已确认写入的观察时钟，使用真实账本换代和业务采样。"""
    async with runtime.ledger.store.transaction() as conn:
        await conn.execute(
            """
            UPDATE remediation_jobs
            SET effective_at = clock_timestamp() - interval '20 minutes',
                run_after = clock_timestamp()
            WHERE proposal_id = %s AND state = 'observing'
            """,
            (proposal_id,),
        )
    await runtime.drive(proposal_id)


async def drive_replica(runtime, proposal_id):
    await runtime.drive(proposal_id)
    await runtime.drive(proposal_id)
    await finish_observation(runtime, proposal_id)
    return await runtime.ledger.store.get_attempt(proposal_id)


def intent_for(proposal):
    return ActionAttempt(
        id=f"att-{uuid4().hex}",
        proposal_id=proposal.id,
        idempotency_key=proposal.idempotency_key,
        status=AttemptStatus.INTENT,
        started_at=datetime.now(UTC),
        snapshot={},
        request={},
    )


async def expire_job(store, proposal_id):
    async with store.transaction() as conn:
        await conn.execute(
            """
            UPDATE remediation_jobs
            SET expires_at = %s, run_after = clock_timestamp()
            WHERE proposal_id = %s AND owner IS NOT NULL
            """,
            (datetime.now(UTC) - timedelta(minutes=3), proposal_id),
        )
