"""动作测试共用的假适配器、提案构造和执行辅助。"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from db_env import DSN, TEST_REASON

from aiops.actions.executor import (
    ActionExecutor,
    AutoExecutionError,
)
from aiops.actions.restart_pod import PodPlan
from aiops.actions.rollout_undo import ActionError, RolloutPlan
from aiops.contracts import (
    ActionKind,
    ActionOrigin,
    ActionProposal,
    ActionTarget,
    TimeRange,
    ToolResult,
)
from aiops.db import Store

START = datetime.now(UTC)
WINDOW = TimeRange(start=START, end=START + timedelta(minutes=1))


def make_proposal(**overrides) -> ActionProposal:
    values = {
        "id": "act-test",
        "kind": ActionKind.ROLLOUT_UNDO,
        "target": ActionTarget(
            cluster="vigil-lab",
            namespace="vigil-shop",
            kind="Deployment",
            name="productcatalogservice",
            uid="uid-1",
            resource_version="10",
        ),
        "parameters": {"from_revision": 2, "to_revision": 1},
        "idempotency_key": "uid-1:2",
        "reason": "坏发布",
        "origin": ActionOrigin.OPERATOR,
        "created_at": START,
    }
    values.update(overrides)
    return ActionProposal(**values)


class FakeAdapter:
    def __init__(self, plan, *, fail_on_apply=False, runtime=None):
        self._plan = plan
        self._fail_on_apply = fail_on_apply
        self._runtime = runtime
        self.calls = []

    def plan(self, namespace, name):
        return self._plan

    def apply(self, plan, *, dry_run, proposal_id):
        self.calls.append(dry_run)
        if self._fail_on_apply and not dry_run:
            raise RuntimeError("连接中断")
        return {"revision": plan.target_revision, "dry_run": dry_run}

    def wait_rolled_out(self, plan, timeout_seconds):
        return True

    def state(self, namespace, name):
        if self._runtime is not None:
            return self._runtime
        return {
            "memory_limit": self._plan.target_memory_limit,
            "pods": [{"name": "pod-new", "ready": True, "restarts": 0}],
        }


class FakePrometheus:
    """一次列表服务所有查询；列表套列表按调用顺序取值，末项兜底。"""

    def __init__(self, values):
        self._values = values
        self._calls = 0

    async def query_range(self, context, expression, time_range, *, step_seconds):
        values = self._values
        if values and isinstance(values[0], list):
            values = values[min(self._calls, len(values) - 1)]
        self._calls += 1
        return ToolResult(
            tool_call_id=f"call-{uuid4().hex[:8]}",
            source="prometheus",
            query={"expression": expression},
            time_range=time_range,
            collected_at=datetime.now(UTC),
            data={
                "series": [
                    {
                        "labels": {},
                        "points": [[float(index), value] for index, value in enumerate(values)],
                    }
                ]
            },
            raw_ref="evidence/synthetic/verify.json",
        )


def rollout_plan(*, current_memory_limit=None, target_memory_limit=None):
    return RolloutPlan(
        namespace="vigil-shop",
        name="productcatalogservice",
        current_revision=2,
        target_revision=1,
        current_image="vigil/productcatalogservice-bad:exp",
        target_image="gcr.io/productcatalogservice@sha256:good",
        template={"metadata": {}, "spec": {"containers": [{"name": "server"}]}},
        deployment_uid="uid-1",
        resource_version="10",
        current_memory_limit=current_memory_limit,
        target_memory_limit=target_memory_limit,
    )


def build_executor(
    store,
    adapter,
    prometheus,
    tmp_path,
    *,
    approval_ttl_seconds=900,
    restart_adapter=None,
    verify_seconds=60,
    sample_seconds=15,
    cluster="vigil-lab",
):
    return ActionExecutor(
        store=store,
        adapter=adapter,
        restart_adapter=restart_adapter,
        prometheus=prometheus,
        runtime_path=tmp_path,
        cluster=cluster,
        namespace="vigil-shop",
        verify_seconds=verify_seconds,
        sample_seconds=sample_seconds,
        approval_ttl_seconds=approval_ttl_seconds,
        verify_delay_seconds=0,
        verify_grace_seconds=0,
    )


async def propose_and_approve(adapter, prometheus, tmp_path, cleanup):
    async with Store.open(DSN) as store:
        await store.migrate()
        executor = build_executor(store, adapter, prometheus, tmp_path)
        proposal, decision = await executor.propose_rollback(
            deployment="productcatalogservice", reason="确认坏发布", actor="tester"
        )
        cleanup.append(proposal.id)
        attempt = await executor.approve(proposal.id, actor="tester", reason="同意")
        saved = await store.get_proposal(proposal.id)
        approval = await store.get_approval(proposal.id)
    return proposal, decision, attempt, saved, approval


def pod_plan(**overrides) -> PodPlan:
    values = {
        "namespace": "vigil-shop",
        "name": "rebuild-demo-abc",
        "uid": "pod-uid-1",
        "resource_version": "100",
        "owner_kind": "ReplicaSet",
        "owner_name": "rebuild-demo-5f6",
        "owner_uid": "rs-uid-1",
        "template_hash": "tmpl-1",
        "spec_hash": "spec-1",
        "sibling_uids": (),
        "restart_count": 0,
        "ready": True,
        "image": "vigil/pod-rebuild-demo:20261105",
        "discardable": True,
        "ip": "10.42.0.9",
    }
    values.update(overrides)
    return PodPlan(**values)


class FakeRestartAdapter:
    def __init__(
        self,
        plan,
        *,
        rebuilt_spec_hash=None,
        fail_on_delete=False,
        replan=None,
        service_status=200,
        service_selects=True,
        service_selects_after=None,
        service_selects_after_call=1,
        service_selects_not_ready=False,
        live_ready=True,
        live_restarts=0,
        rebuild_available=True,
        replan_error=None,
        replan_after=1,
        statuses=None,
        fail_on_dry_run=False,
    ):
        self._plan = plan
        self._rebuilt_spec_hash = rebuilt_spec_hash or plan.spec_hash
        self._fail_on_delete = fail_on_delete
        self._replan = replan
        self._service_status = service_status
        self._statuses = list(statuses) if statuses is not None else None
        self._service_selects = service_selects
        self._service_selects_after = service_selects_after
        self._service_selects_after_call = service_selects_after_call
        self._service_selects_not_ready = service_selects_not_ready
        self._live_ready = live_ready
        self._live_restarts = live_restarts
        self._rebuild_available = rebuild_available
        self._replan_error = replan_error
        self._replan_after = replan_after
        self._fail_on_dry_run = fail_on_dry_run
        self.calls = []
        self._plan_calls = 0
        self._service_calls = 0
        self.pod_get_calls = 0

    def plan(self, namespace, name):
        self._plan_calls += 1
        if self._replan_error is not None and self._plan_calls > self._replan_after:
            raise self._replan_error
        if self._replan is not None and self._plan_calls > self._replan_after:
            return self._replan
        return self._plan

    def applicability(self, namespace, name):
        """只读事实；默认没有 OOM 或模板规格线索，不改变既有用例语义。"""
        return {
            "uid": self._plan.uid,
            "phase": "Running",
            "ready": self._plan.ready,
            "restart_count": self._plan.restart_count,
            "last_terminated_reason": None,
            "last_terminated_exit_code": None,
            "memory_limit": None,
            "memory_request": None,
            "image": self._plan.image,
        }

    def dry_run(self, plan):
        self.calls.append(("dry_run", plan.uid))
        if self._fail_on_dry_run:
            raise ActionError("precondition_failed", "dry-run 失败")
        return {"dry_run": True}

    def execute(self, plan):
        self.calls.append(("execute", plan.uid))
        if self._fail_on_delete:
            raise RuntimeError("连接中断")
        return {"deleted": plan.name, "uid": plan.uid}

    def wait_rebuilt(self, plan, timeout_seconds):
        self.calls.append(("wait", plan.uid))
        if not self._rebuild_available:
            return None
        return {
            "name": "rebuild-demo-def",
            "uid": "pod-uid-2",
            "ready": True,
            "restarts": 0,
            "template_hash": self._rebuilt_spec_hash,
            "ip": "10.42.0.9",
            "phase": "Running",
        }

    def pod_get(self, namespace, name, path):
        self.pod_get_calls += 1
        if self._statuses is not None:
            status = self._statuses[min(self.pod_get_calls - 1, len(self._statuses) - 1)]
        else:
            status = self._service_status
        return {"ok": status == 200, "status": status, "body": "ok" if status == 200 else ""}

    def service_selects(self, namespace, service, pod_ip, *, ready_only=True):
        self._service_calls += 1
        ok = self._service_selects
        if self._service_selects_not_ready:
            # 目标只出现在 notReadyAddresses：选择关系成立，但还没有就绪端点。
            ok = not ready_only
        if (
            self._service_selects_after is not None
            and self._service_calls > self._service_selects_after_call
        ):
            ok = self._service_selects_after
        return {"ok": ok, "pod_ip": pod_ip, "addresses": [pod_ip], "ready_only": ready_only}

    def pod_state(self, namespace, owner_uid, uid):
        return {
            "name": "rebuild-demo-def",
            "uid": uid,
            "ready": self._live_ready,
            "restarts": self._live_restarts,
            "template_hash": self._rebuilt_spec_hash,
            "ip": "10.42.0.9",
            "phase": "Running",
        }


def restart_proposal(**overrides) -> ActionProposal:
    values = {
        "kind": ActionKind.RESTART_POD,
        "target": ActionTarget(
            cluster="vigil-lab",
            namespace="vigil-shop",
            kind="Pod",
            name="rebuild-demo-abc",
            uid="pod-uid-1",
            resource_version="100",
        ),
        "parameters": {
            "pod": "rebuild-demo-abc",
            "replica_set": "rebuild-demo-5f6",
            "owner_kind": "ReplicaSet",
            "discardable": True,
            "template_hash": "tmpl-1",
            "service": "rebuild-demo",
            "path": "/work",
        },
        "idempotency_key": "restart:pod-uid-1",
        "reason": "Pod 级状态损坏",
    }
    values.update(overrides)
    return make_proposal(**values)


def experiment_restart_proposal() -> ActionProposal:
    proposal = restart_proposal()
    return proposal.model_copy(
        update={"target": proposal.target.model_copy(update={"namespace": "vigil-experiment"})}
    )


async def run_restart(
    adapter,
    tmp_path,
    cleanup,
    *,
    verify_seconds=1,
    sample_seconds=0.2,
    namespace="vigil-shop",
    approve=True,
    origin=ActionOrigin.OPERATOR,
    evidence_ids=None,
    cluster="vigil-lab",
):
    async with Store.open(DSN) as store:
        await store.migrate()
        executor = build_executor(
            store,
            FakeAdapter(rollout_plan()),
            FakePrometheus([200.0]),
            tmp_path,
            restart_adapter=adapter,
            verify_seconds=verify_seconds,
            sample_seconds=sample_seconds,
            cluster=cluster,
        )
        try:
            proposal, decision = await executor.propose_restart_pod(
                pod="rebuild-demo-abc",
                service="rebuild-demo",
                path="/work",
                reason=TEST_REASON,
                actor="tester",
                namespace=namespace,
                origin=origin,
                evidence_ids=evidence_ids,
            )
        except AutoExecutionError as exc:
            cleanup.append(exc.proposal_id)
            raise
        cleanup.append(proposal.id)
        attempt = await store.get_attempt(proposal.id)
        if approve:
            try:
                attempt = await executor.approve(proposal.id, actor="tester", reason="同意")
            except ActionError:
                attempt = None
        saved = await store.get_proposal(proposal.id)
    return proposal, decision, attempt, saved


def read_followup(attempt_id):
    async def run():
        async with Store.open(DSN) as store:
            return await store.get_followup_by_attempt(attempt_id)

    return asyncio.run(run())
