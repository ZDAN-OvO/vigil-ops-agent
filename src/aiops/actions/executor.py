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
from aiops.db import DuplicateAttempt, DuplicateProposal, Store
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
        approval_ttl_seconds: int = 900,
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
        self._approval_ttl = approval_ttl_seconds
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
        origin: ActionOrigin = ActionOrigin.OPERATOR,
        evidence_ids: list[str] | None = None,
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
                "from_memory_limit": plan.current_memory_limit,
                "to_memory_limit": plan.target_memory_limit,
            },
            idempotency_key=f"{plan.deployment_uid}:{plan.current_revision}",
            reason=reason,
            origin=origin,
            investigation_id=investigation_id,
            evidence_ids=evidence_ids or [],
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

    async def approve(
        self, proposal_id: str, *, actor: str, reason: str = "", defer: bool = False
    ) -> ActionAttempt | None:
        """记录批准；默认立即执行，defer 时只留下可跨进程继续的授权。"""
        approval = await self._record_decision(
            proposal_id, actor=actor, reason=reason, decision=ApprovalDecision.APPROVED
        )
        if defer:
            return None
        proposal = await self._store.get_proposal(proposal_id)
        if proposal is None:
            raise ActionError("not_found", f"提案不存在：{proposal_id}")
        return await self._execute(proposal, approval)

    async def reject(self, proposal_id: str, *, actor: str, reason: str = "") -> ActionProposal:
        await self._record_decision(
            proposal_id, actor=actor, reason=reason, decision=ApprovalDecision.REJECTED
        )
        proposal = await self._store.get_proposal(proposal_id)
        if proposal is None:
            raise LookupError(f"提案不存在：{proposal_id}")
        return proposal

    async def resume(self, proposal_id: str, *, actor: str = "operator") -> ActionAttempt:
        """继续执行已批准但尚未执行的提案；重新校验授权、有效期与目标。"""
        proposal = await self._store.get_proposal(proposal_id)
        if proposal is None:
            raise ActionError("not_found", f"提案不存在：{proposal_id}")
        if await self._store.get_attempt(proposal_id) is not None:
            raise ActionError("already_executed", "提案已有执行记录，不重复写")
        if proposal.status is not ProposalStatus.APPROVED:
            raise ActionError("not_approved", f"提案状态为 {proposal.status.value}，不能继续执行")
        approval = await self._store.get_approval(proposal_id)
        if approval is None or approval.decision is not ApprovalDecision.APPROVED:
            raise ActionError("no_approval", "找不到有效的批准记录，不能执行")
        await self._audit(proposal.id, actor, "action.resumed", {"approval_id": approval.id})
        return await self._execute(proposal, approval)

    async def _record_decision(
        self, proposal_id: str, *, actor: str, reason: str, decision: ApprovalDecision
    ) -> Approval:
        proposal = await self._require_pending(proposal_id)
        target = (
            ProposalStatus.APPROVED
            if decision is ApprovalDecision.APPROVED
            else ProposalStatus.REJECTED
        )
        # 状态迁移是唯一入口：并发审批只有一个进程抢到 pending → approved／rejected。
        if not await self._store.transition_proposal(
            proposal_id, expected=ProposalStatus.PENDING_APPROVAL, target=target
        ):
            current = await self._store.get_proposal(proposal_id)
            status = current.status.value if current is not None else "missing"
            raise ActionError("not_pending", f"提案状态为 {status}，不能审批")
        decided_at = datetime.now(UTC)
        approval = Approval(
            id=f"apr-{uuid.uuid4().hex[:12]}",
            proposal_id=proposal.id,
            proposal_hash=proposal_hash(proposal),
            actor=actor,
            decision=decision,
            reason=reason,
            decided_at=decided_at,
            expires_at=(
                decided_at + timedelta(seconds=self._approval_ttl)
                if decision is ApprovalDecision.APPROVED
                else None
            ),
        )
        await self._store.save_approval(approval)
        await self._audit(
            proposal.id,
            actor,
            f"action.{decision.value}",
            {
                "approval_id": approval.id,
                "proposal_hash": approval.proposal_hash,
                "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
            },
        )
        return approval

    async def _require_pending(self, proposal_id: str) -> ActionProposal:
        proposal = await self._store.get_proposal(proposal_id)
        if proposal is None:
            raise ActionError("not_found", f"提案不存在：{proposal_id}")
        if proposal.status is not ProposalStatus.PENDING_APPROVAL:
            raise ActionError("not_pending", f"提案状态为 {proposal.status.value}，不能审批")
        return proposal

    async def _execute(self, proposal: ActionProposal, approval: Approval) -> ActionAttempt:
        if await self._store.get_attempt(proposal.id) is not None:
            raise ActionError("already_executed", "提案已有执行记录，不重复写")
        if approval.decision is not ApprovalDecision.APPROVED:
            raise ActionError("no_approval", "拒绝记录不能用于执行")
        if approval.expires_at is not None and datetime.now(UTC) > approval.expires_at:
            raise await self._fail(
                proposal, "expired", "审批已过期，需重新提案", ProposalStatus.EXPIRED
            )
        if approval.proposal_hash != proposal_hash(proposal):
            raise await self._fail(proposal, "hash_mismatch", "审批后提案内容已变化，授权失效")

        key = target_key(
            proposal.target.cluster,
            proposal.target.namespace,
            proposal.target.kind,
            proposal.target.name,
        )
        if not await self._store.acquire_lock(key, proposal.id):
            # 目标忙不是本提案的失败，保留原状态，避免覆盖正在执行的进程。
            await self._audit(
                proposal.id, "action-executor", "action.blocked", {"code": "target_busy"}
            )
            raise ActionError("target_busy", "目标已有未完成的执行，本次不重放")
        try:
            try:
                plan = await asyncio.to_thread(
                    self._adapter.plan, proposal.target.namespace, proposal.target.name
                )
            except ActionError as exc:
                raise await self._fail(proposal, exc.code, exc.message) from exc

            if (
                plan.deployment_uid != proposal.target.uid
                or plan.resource_version != proposal.target.resource_version
            ):
                raise await self._fail(proposal, "drift", "审批后目标资源已变化，需重新提案")

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
            try:
                await self._store.save_attempt(attempt)
            except DuplicateAttempt as exc:
                raise ActionError("already_executed", str(exc)) from exc
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
            runtime = await self._runtime_state(proposal)
            ready = [pod for pod in runtime.get("pods", []) if pod.get("ready")]
            restarts = sum(int(pod.get("restarts") or 0) for pod in ready)
            # 无内存限额目标时跳过该检查；有目标就必须在窗口内回到目标规格且无重启。
            limit_ok = (
                plan.target_memory_limit is None
                or runtime.get("memory_limit") == plan.target_memory_limit
            )
            ok = (
                rolled_out
                and verification.get("ok") is True
                and limit_ok
                and bool(ready)
                and restarts == 0
            )
            verification = {
                **verification,
                "runtime": runtime,
                "memory_limit_restored": limit_ok,
                "ready_pods": len(ready),
                "pod_restarts": restarts,
            }
            return await self._finish(
                attempt,
                AttemptStatus.VERIFIED if ok else AttemptStatus.FAILED,
                ProposalStatus.SUCCEEDED if ok else ProposalStatus.FAILED,
                result={"stage": "applied", "rolled_out": rolled_out},
                verification=verification,
            )
        finally:
            await self._store.release_lock(key, proposal.id)

    async def _runtime_state(self, proposal: ActionProposal) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                self._adapter.state, proposal.target.namespace, proposal.target.name
            )
        except Exception as exc:  # noqa: BLE001 - 读不到运行态时按未验证处理
            return {"error": exc.__class__.__name__}

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

    async def _fail(
        self,
        proposal: ActionProposal,
        code: str,
        message: str,
        status: ProposalStatus = ProposalStatus.FAILED,
    ) -> ActionError:
        """在写任何外部请求前结束提案，并把判定原因落审计。"""
        await self._store.set_proposal_status(proposal.id, status)
        await self._audit(proposal.id, "action-executor", f"action.{status.value}", {"code": code})
        return ActionError(code, message)

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
