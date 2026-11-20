"""离线验证 Pod 重建授权后的适用性复查。"""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from actions_support import (
    DSN,
    FakeAdapter,
    FakePrometheus,
    FakeRestartAdapter,
    build_executor,
    pod_plan,
    rollout_plan,
)

from aiops.actions.outcome import SampleSummary
from aiops.actions.rollout_undo import ActionError
from aiops.contracts import ProposalStatus
from aiops.db import Store

pytestmark = pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL")


class DriftingRestart(FakeRestartAdapter):
    def __init__(self, *, drift_at):
        super().__init__(pod_plan())
        self.drift_at = drift_at
        self.oom = False

    def drift(self, *, oom=True):
        self.oom = oom
        self._plan = replace(self._plan, resource_version="101", restart_count=3 if oom else 0)

    def applicability(self, namespace, name):
        return {
            **super().applicability(namespace, name),
            "last_terminated_reason": "OOMKilled" if self.oom else None,
            "memory_limit": "128Mi",
        }

    def dry_run(self, plan):
        result = super().dry_run(plan)
        if self.drift_at == "dry_run":
            self.drift()
        return result


async def approved_restart(store, tmp_path, monkeypatch, cleanup, *, drift_at):
    adapter = DriftingRestart(drift_at=drift_at)
    executor = await build_executor(
        store,
        FakeAdapter(rollout_plan()),
        FakePrometheus([500.0]),
        tmp_path,
        cleanup=cleanup,
        restart_adapter=adapter,
    )
    proposal, _, _ = await executor.prepare_restart_pod(
        pod=adapter._plan.name,
        service="rebuild-demo",
        path="/work",
        reason="测试：审批后重新核对适用性",
        actor="tester",
    )
    cleanup.append(proposal.id)
    await executor.approve(proposal.id, actor="tester", defer=True)

    async def baseline(*args):
        if drift_at == "baseline":
            adapter.drift()
        return SampleSummary(samples=3, failed=3)

    monkeypatch.setattr(executor, "_restart_baseline", baseline)
    monkeypatch.setattr(
        executor,
        "_verify_restart",
        AsyncMock(
            return_value={
                "ok": True,
                "samples": 3,
                "failed_samples": 0,
                "tail_samples": 3,
                "tail_failed_samples": 0,
                "tail_unavailable_samples": 0,
                "service_selects_new_pod": True,
            }
        ),
    )
    return executor, adapter, proposal


@pytest.mark.parametrize("drift_at", ["approval", "baseline", "dry_run"])
def test_restart_rechecks_oom_before_write(tmp_path, monkeypatch, cleanup, drift_at):
    async def scenario():
        async with Store.open(DSN) as store:
            await store.migrate()
            executor, adapter, proposal = await approved_restart(
                store,
                tmp_path,
                monkeypatch,
                cleanup,
                drift_at=drift_at,
            )
            if drift_at == "approval":
                adapter.drift()
            with pytest.raises(ActionError) as caught:
                await executor.resume(proposal.id)
            assert caught.value.code == "oom_template_inapplicable"
            assert proposal.target.resource_version == "100"
            assert adapter._plan.uid == proposal.target.uid
            assert adapter._plan.resource_version == "101"
            assert adapter._plan.restart_count == 3
            expected = ["dry_run"] if drift_at == "dry_run" else []
            assert [call[0] for call in adapter.calls] == expected
            assert await store.get_attempt(proposal.id) is None
            assert (await store.get_proposal(proposal.id)).status is ProposalStatus.FAILED

    asyncio.run(scenario())


def test_restart_allows_rv_drift_when_still_applicable(tmp_path, monkeypatch, cleanup):
    async def scenario():
        async with Store.open(DSN) as store:
            await store.migrate()
            executor, adapter, proposal = await approved_restart(
                store,
                tmp_path,
                monkeypatch,
                cleanup,
                drift_at=None,
            )
            adapter.drift(oom=False)
            await executor.resume(proposal.id)
            assert proposal.target.resource_version == "100"
            assert adapter._plan.resource_version == "101"
            assert [call[0] for call in adapter.calls] == ["dry_run", "execute", "wait"]

    asyncio.run(scenario())
