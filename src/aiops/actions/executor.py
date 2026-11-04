"""受控执行器：提案 → 审批 → 再次检查 → 执行 → 业务验证。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aiops.actions.policy import decide, target_key
from aiops.actions.rollout_undo import ActionError, RolloutUndoAdapter
from aiops.contracts import (
    ActionAttempt,
    ActionKind,
    ActionOrigin,
    ActionProposal,
    ActionTarget,
    Approval,
    ApprovalDecision,
    AttemptStatus,
    AuditEvent,
    ProposalStatus,
    RiskDecision,
    RiskDisposition,
    TimeRange,
)
from aiops.db import DuplicateProposal, Store
from aiops.tools.base import ToolContext
from aiops.tools.prometheus import PrometheusClient


def proposal_hash(proposal: ActionProposal) -> str:
    payload = {
        "id": proposal.id,
        "kind": proposal.kind.value,
        "target": proposal.target.model_dump(mode="json"),
        "parameters": proposal.parameters,
        "idempotency_key": proposal.idempotency_key,
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ActionExecutor:
    def __init__(
        self,
        *,
        store: Store,
        adapter: RolloutUndoAdapter,
        prometheus: PrometheusClient,
        runtime_path: Path,
        cluster: str,
        namespace: str,
        rollout_timeout_seconds: float = 180.0,
        verify_seconds: int = 90,
        sample_seconds: int = 15,
        verify_delay_seconds: float | None = None,
        verify_grace_seconds: float | None = None,
        probe_expression: str = 'probe_success{job="frontend-probe"}',
    ) -> None:
        self._store = store
        self._adapter = adapter
        self._prometheus = prometheus
        self._runtime_path = runtime_path
        self._cluster = cluster
        self._namespace = namespace
        self._rollout_timeout = rollout_timeout_seconds
        self._verify_seconds = verify_seconds
        self._sample_seconds = sample_seconds
        self._verify_delay = (
            float(verify_seconds) if verify_delay_seconds is None else verify_delay_seconds
        )
        self._verify_grace = (
            float(sample_seconds * 2) if verify_grace_seconds is None else verify_grace_seconds
        )
        self._probe_expression = probe_expression
        self._min_samples = max(2, verify_seconds // max(1, sample_seconds) // 2)

    async def propose_rollback(
        self,
        *,
        deployment: str,
        reason: str,
        actor: str,
        namespace: str | None = None,
        investigation_id: str | None = None,
    ) -> tuple[ActionProposal, RiskDecision]:
        namespace = namespace or self._namespace
        plan = await asyncio.to_thread(self._adapter.plan, namespace, deployment)
        proposal = ActionProposal(
            id=f"act-{uuid.uuid4().hex[:12]}",
            kind=ActionKind.ROLLOUT_UNDO,
            target=ActionTarget(
                cluster=self._cluster,
                namespace=namespace,
                kind="Deployment",
                name=deployment,
                uid=plan.deployment_uid,
                resource_version=plan.resource_version,
            ),
            parameters={
                "from_revision": plan.current_revision,
                "to_revision": plan.target_revision,
                "target_image": plan.target_image,
            },
            idempotency_key=f"{plan.deployment_uid}:{plan.current_revision}",
            reason=reason,
            origin=ActionOrigin.OPERATOR,
            investigation_id=investigation_id,
            created_at=datetime.now(UTC),
        )
        key = target_key(self._cluster, namespace, "Deployment", deployment)
        decision = decide(proposal, target_busy=await self._store.target_is_locked(key))
        status = (
            ProposalStatus.DENIED
            if decision.disposition is RiskDisposition.DENY
            else ProposalStatus.PENDING_APPROVAL
        )
        proposal = proposal.model_copy(update={"status": status})
        try:
            await self._store.save_proposal(proposal, decision)
        except DuplicateProposal as exc:
            raise ActionError("duplicate", str(exc)) from exc
        await self._audit(
            proposal.id,
            actor,
            "action.proposed",
            {
                "kind": proposal.kind.value,
                "target": f"{namespace}/{deployment}",
                "to_revision": plan.target_revision,
                "disposition": decision.disposition.value,
                "reasons": decision.reasons,
            },
        )
        return proposal, decision

    async def approve(self, proposal_id: str, *, actor: str, reason: str = "") -> ActionAttempt:
        proposal = await self._require_pending(proposal_id)
        approval = Approval(
            id=f"apr-{uuid.uuid4().hex[:12]}",
            proposal_id=proposal.id,
            proposal_hash=proposal_hash(proposal),
            actor=actor,
            decision=ApprovalDecision.APPROVED,
            reason=reason,
            decided_at=datetime.now(UTC),
        )
        await self._store.save_approval(approval)
        await self._store.set_proposal_status(proposal.id, ProposalStatus.APPROVED)
        await self._audit(
            proposal.id, actor, "action.approved", {"proposal_hash": approval.proposal_hash}
        )
        return await self._execute(proposal, approval)

    async def reject(self, proposal_id: str, *, actor: str, reason: str = "") -> ActionProposal:
        proposal = await self._require_pending(proposal_id)
        approval = Approval(
            id=f"apr-{uuid.uuid4().hex[:12]}",
            proposal_id=proposal.id,
            proposal_hash=proposal_hash(proposal),
            actor=actor,
            decision=ApprovalDecision.REJECTED,
            reason=reason,
            decided_at=datetime.now(UTC),
        )
        await self._store.save_approval(approval)
        await self._store.set_proposal_status(proposal.id, ProposalStatus.REJECTED)
        await self._audit(proposal.id, actor, "action.rejected", {"reason": reason})
        return proposal.model_copy(update={"status": ProposalStatus.REJECTED})

    async def _require_pending(self, proposal_id: str) -> ActionProposal:
        proposal = await self._store.get_proposal(proposal_id)
        if proposal is None:
            raise ActionError("not_found", f"提案不存在：{proposal_id}")
        if proposal.status is not ProposalStatus.PENDING_APPROVAL:
            raise ActionError("not_pending", f"提案状态为 {proposal.status.value}，不能审批")
        return proposal

    async def _execute(self, proposal: ActionProposal, approval: Approval) -> ActionAttempt:
        key = target_key(
            proposal.target.cluster,
            proposal.target.namespace,
            proposal.target.kind,
            proposal.target.name,
        )
        if not await self._store.acquire_lock(key, proposal.id):
            await self._store.set_proposal_status(proposal.id, ProposalStatus.FAILED)
            raise ActionError("target_busy", "目标已有未完成的执行，本次不重放")
        try:
            try:
                plan = await asyncio.to_thread(
                    self._adapter.plan, proposal.target.namespace, proposal.target.name
                )
            except ActionError as exc:
                await self._store.set_proposal_status(proposal.id, ProposalStatus.FAILED)
                await self._audit(
                    proposal.id, "action-executor", "action.failed", {"code": exc.code}
                )
                raise

            if (
                plan.deployment_uid != proposal.target.uid
                or plan.resource_version != proposal.target.resource_version
            ):
                await self._store.set_proposal_status(proposal.id, ProposalStatus.FAILED)
                await self._audit(
                    proposal.id, "action-executor", "action.failed", {"code": "drift"}
                )
                raise ActionError("drift", "审批后目标资源已变化，需重新提案")

            attempt = ActionAttempt(
                id=f"att-{uuid.uuid4().hex[:12]}",
                proposal_id=proposal.id,
                status=AttemptStatus.INTENT,
                idempotency_key=proposal.idempotency_key,
                snapshot={
                    "uid": plan.deployment_uid,
                    "resource_version": plan.resource_version,
                    "from_revision": plan.current_revision,
                    "to_revision": plan.target_revision,
                    "current_image": plan.current_image,
                    "target_image": plan.target_image,
                    "approval_hash": approval.proposal_hash,
                },
                request={
                    "kind": "rollout_undo",
                    "namespace": plan.namespace,
                    "name": plan.name,
                    "revision": plan.target_revision,
                },
                started_at=datetime.now(UTC),
            )
            await self._store.save_attempt(attempt)
            await self._store.set_proposal_status(proposal.id, ProposalStatus.EXECUTING)

            try:
                await asyncio.to_thread(
                    self._adapter.apply, plan, dry_run=True, proposal_id=proposal.id
                )
            except ActionError as exc:
                return await self._finish(
                    attempt,
                    AttemptStatus.FAILED,
                    ProposalStatus.FAILED,
                    result={"stage": "dry_run", "code": exc.code, "message": exc.message},
                )

            await self._store.update_attempt(
                attempt.id,
                status=AttemptStatus.EXECUTING,
                result={"stage": "apply_started"},
            )
            try:
                await asyncio.to_thread(
                    self._adapter.apply, plan, dry_run=False, proposal_id=proposal.id
                )
            except Exception as exc:  # noqa: BLE001 - 结果未知时停止，不重放写请求
                return await self._finish(
                    attempt,
                    AttemptStatus.UNKNOWN,
                    ProposalStatus.UNKNOWN,
                    result={"stage": "apply", "error": exc.__class__.__name__},
                )

            rolled_out = await asyncio.to_thread(
                self._adapter.wait_rolled_out, plan, self._rollout_timeout
            )
            # 滚动完成后再等两个抓取周期，避免把恢复前的最后一个样本算进来。
            await asyncio.sleep(self._verify_grace)
            recovered_from = datetime.now(UTC)
            await asyncio.sleep(self._verify_delay)
            verification = await self._verify_business(recovered_from)
            ok = rolled_out and verification.get("ok") is True
            return await self._finish(
                attempt,
                AttemptStatus.VERIFIED if ok else AttemptStatus.FAILED,
                ProposalStatus.SUCCEEDED if ok else ProposalStatus.FAILED,
                result={"stage": "applied", "rolled_out": rolled_out},
                verification=verification,
            )
        finally:
            await self._store.release_lock(key, proposal.id)

    async def _verify_business(self, since: datetime) -> dict[str, Any]:
        """只看执行之后的样本；故障窗口内的失败不能算作恢复，也不能算成功。"""
        end = datetime.now(UTC)
        start = max(since, end - timedelta(seconds=self._verify_seconds))
        context = ToolContext.create(
            investigation_id=f"action-check-{self._cluster}",
            cluster=self._cluster,
            namespace=self._namespace,
            runtime_path=self._runtime_path,
            timeout_seconds=30,
        )
        result = await self._prometheus.query_range(
            context,
            self._probe_expression,
            TimeRange(start=start, end=end),
            step_seconds=self._sample_seconds,
        )
        if result.error is not None:
            return {"ok": False, "reason": "query_failed", "error": result.error.code}
        points = [
            point
            for series in (result.data or {}).get("series", [])
            for point in series.get("points", [])
        ]
        if len(points) < self._min_samples:
            return {"ok": False, "reason": "insufficient_data", "samples": len(points)}
        failed = [point for point in points if point[1] != 1.0]
        # 恢复判定看窗口末段的连续样本；滚动刚结束时的预热失败单独记录，不算恢复失败。
        tail = points[-self._min_samples :]
        tail_failures = sum(1 for point in tail if point[1] != 1.0)
        return {
            "ok": tail_failures == 0,
            "samples": len(points),
            "failed_samples": len(failed),
            "tail_failures": tail_failures,
            "window_seconds": self._verify_seconds,
            "raw_ref": result.raw_ref,
        }

    async def _finish(
        self,
        attempt: ActionAttempt,
        status: AttemptStatus,
        proposal_status: ProposalStatus,
        *,
        result: dict[str, Any] | None = None,
        verification: dict[str, Any] | None = None,
    ) -> ActionAttempt:
        finished_at = datetime.now(UTC)
        await self._store.update_attempt(
            attempt.id,
            status=status,
            result=result,
            verification=verification,
            finished_at=finished_at,
        )
        await self._store.set_proposal_status(attempt.proposal_id, proposal_status)
        await self._audit(
            attempt.proposal_id,
            "action-executor",
            f"action.{proposal_status.value}",
            {
                "attempt_id": attempt.id,
                "attempt_status": status.value,
                "verification": verification,
            },
        )
        updated = await self._store.get_attempt(attempt.proposal_id)
        if updated is None:
            raise LookupError(f"执行记录不存在：{attempt.id}")
        return updated

    async def _audit(
        self, correlation_id: str, actor: str, event: str, payload: dict[str, Any]
    ) -> None:
        await self._store.append_audit(
            [
                AuditEvent(
                    id=f"audit-{uuid.uuid4().hex[:12]}",
                    correlation_id=correlation_id,
                    actor=actor,
                    event=event,
                    occurred_at=datetime.now(UTC),
                    payload=payload,
                )
            ]
        )
