"""离线验证 Pod 重建授权后的适用性复查。"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from actions_support import FakeAdapter, FakePrometheus, FakeRestartAdapter, pod_plan, rollout_plan

from aiops.actions.executor import ActionExecutor, proposal_hash
from aiops.actions.outcome import SampleSummary
from aiops.actions.rollout_undo import ActionError
from aiops.contracts import Approval, ApprovalDecision, ProposalStatus


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


async def approved_restart(tmp_path, monkeypatch, *, drift_at):
    store = AsyncMock()
    store.target_is_locked.return_value = False
    store.latest_proposal_blocks_auto.return_value = False
    store.get_attempt.return_value = None
    store.acquire_lock.return_value = True
    adapter = DriftingRestart(drift_at=drift_at)
    executor = ActionExecutor(
        store=store,
        adapter=FakeAdapter(rollout_plan()),
        restart_adapter=adapter,
        prometheus=FakePrometheus([500.0]),
        runtime_path=tmp_path,
        cluster="vigil-lab",
        namespace="vigil-shop",
    )
    proposal, _, _ = await executor.prepare_restart_pod(
        pod=adapter._plan.name,
        service="rebuild-demo",
        path="/work",
        reason="测试：审批后重新核对适用性",
        actor="tester",
    )
    proposal.status = ProposalStatus.APPROVED
    approval = Approval(
        id="apr-boundary",
        proposal_id=proposal.id,
        proposal_hash=proposal_hash(proposal),
        actor="operator",
        decision=ApprovalDecision.APPROVED,
        reason="同意",
        decided_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    store.get_proposal.return_value = proposal
    store.get_approval.return_value = approval

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
    monkeypatch.setattr(
        executor, "_finish", AsyncMock(side_effect=lambda attempt, *args, **kwargs: attempt)
    )
    return executor, adapter, proposal, store


@pytest.mark.parametrize("drift_at", ["approval", "baseline", "dry_run"])
def test_restart_rechecks_oom_before_write(tmp_path, monkeypatch, drift_at):
    async def scenario():
        executor, adapter, proposal, store = await approved_restart(
            tmp_path, monkeypatch, drift_at=drift_at
        )
        if drift_at == "approval":
            adapter.drift()
        with pytest.raises(ActionError) as caught:
            await executor.execute_authorized(proposal.id)
        assert caught.value.code == "oom_template_inapplicable"
        assert proposal.target.resource_version == "100"
        assert adapter._plan.uid == proposal.target.uid
        assert adapter._plan.resource_version == "101"
        assert adapter._plan.restart_count == 3
        expected = ["dry_run"] if drift_at == "dry_run" else []
        assert [call[0] for call in adapter.calls] == expected
        store.save_attempt.assert_not_awaited()
        store.set_proposal_status.assert_awaited_with(proposal.id, ProposalStatus.FAILED)

    asyncio.run(scenario())


def test_restart_allows_rv_drift_when_still_applicable(tmp_path, monkeypatch):
    async def scenario():
        executor, adapter, proposal, _ = await approved_restart(
            tmp_path, monkeypatch, drift_at=None
        )
        adapter.drift(oom=False)
        await executor.execute_authorized(proposal.id)
        assert proposal.target.resource_version == "100"
        assert adapter._plan.resource_version == "101"
        assert [call[0] for call in adapter.calls] == ["dry_run", "execute", "wait"]

    asyncio.run(scenario())
