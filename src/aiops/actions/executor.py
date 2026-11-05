"""受控执行器：提案 → 审批 → 再次检查 → 执行 → 业务验证。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from aiops.actions.policy import decide, target_key
from aiops.actions.restart_pod import PodPlan, PodRestartAdapter
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


class AutoExecutionError(ActionError):
    """自动执行在提案与授权落库之后失败；提案仍在，可用 resume 继续。"""

    def __init__(self, code: str, message: str, proposal_id: str) -> None:
        super().__init__(code, message)
        self.proposal_id = proposal_id


def _initial_status(decision: RiskDecision) -> ProposalStatus:
    if decision.disposition is RiskDisposition.DENY:
        return ProposalStatus.DENIED
    if decision.disposition is RiskDisposition.AUTO:
        return ProposalStatus.APPROVED
    return ProposalStatus.PENDING_APPROVAL


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
        restart_adapter: PodRestartAdapter | None = None,
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
        self._restart_adapter = restart_adapter
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
                "workload": plan.deployment_uid,
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
        proposal = proposal.model_copy(update={"status": _initial_status(decision)})
        approval = await self._persist(proposal, decision)
        await self._audit_safe(
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
        return await self._after_decision(proposal, decision, approval)

    async def propose_restart_pod(
        self,
        *,
        pod: str,
        service: str,
        reason: str,
        actor: str,
        path: str = "/",
        namespace: str | None = None,
        investigation_id: str | None = None,
        origin: ActionOrigin = ActionOrigin.OPERATOR,
        evidence_ids: list[str] | None = None,
    ) -> tuple[ActionProposal, RiskDecision]:
        namespace = namespace or self._namespace
        adapter = self._restart_adapter
        if adapter is None:
            raise ActionError("adapter_missing", "未配置 restart_pod 适配器")
        plan = await asyncio.to_thread(adapter.plan, namespace, pod)
        # 工作负载身份跨 revision 稳定；没有 Deployment 归属时退回 ReplicaSet 或 Pod。
        workload = plan.workload_uid or plan.owner_uid or plan.name
        proposal = ActionProposal(
            id=f"act-{uuid.uuid4().hex[:12]}",
            kind=ActionKind.RESTART_POD,
            target=ActionTarget(
                cluster=self._cluster,
                namespace=namespace,
                kind="Pod",
                name=plan.name,
                uid=plan.uid,
                resource_version=plan.resource_version,
            ),
            parameters={
                "pod": plan.name,
                "replica_set": plan.owner_name,
                "owner_kind": plan.owner_kind,
                "workload": workload,
                "workload_name": plan.workload_name,
                "discardable": plan.discardable,
                "template_hash": plan.template_hash,
                "restart_count": plan.restart_count,
                "service": service,
                "path": path,
            },
            idempotency_key=f"restart:{plan.uid}",
            reason=reason,
            origin=origin,
            investigation_id=investigation_id,
            evidence_ids=evidence_ids or [],
            created_at=datetime.now(UTC),
        )
        key = target_key(self._cluster, namespace, "Pod", plan.name)
        decision = decide(
            proposal,
            target_busy=await self._store.target_is_locked(key),
            # 同一工作负载最近一次执行未成功时不再自动放行。
            prior_auto_failure=await self._store.latest_proposal_blocks_auto(
                self._cluster, namespace, workload, plan.owner_name or ""
            ),
        )
        proposal = proposal.model_copy(update={"status": _initial_status(decision)})
        approval = await self._persist(proposal, decision)
        await self._audit_safe(
            proposal.id,
            actor,
            "action.proposed",
            {
                "kind": proposal.kind.value,
                "target": f"{namespace}/{plan.name}",
                "replica_set": plan.owner_name,
                "disposition": decision.disposition.value,
                "reasons": decision.reasons,
            },
        )
        return await self._after_decision(proposal, decision, approval)

    async def _persist(self, proposal: ActionProposal, decision: RiskDecision) -> Approval | None:
        """自动允许时提案与授权同事务写入；返回随提案一起落库的授权。"""
        if decision.disposition is not RiskDisposition.AUTO:
            try:
                await self._store.save_proposal(proposal, decision)
            except DuplicateProposal as exc:
                raise ActionError("duplicate", str(exc)) from exc
            return None
        decided_at = datetime.now(UTC)
        approval = Approval(
            id=f"apr-{uuid.uuid4().hex[:12]}",
            proposal_id=proposal.id,
            proposal_hash=proposal_hash(proposal),
            actor=f"policy:{decision.policy_version}",
            decision=ApprovalDecision.APPROVED,
            reason="自动允许：" + "；".join(decision.reasons),
            decided_at=decided_at,
            expires_at=decided_at + timedelta(seconds=self._approval_ttl),
        )
        try:
            await self._store.save_proposal_with_approval(proposal, decision, approval)
        except DuplicateProposal as exc:
            raise ActionError("duplicate", str(exc)) from exc
        return approval

    async def _after_decision(
        self, proposal: ActionProposal, decision: RiskDecision, approval: Approval | None
    ) -> tuple[ActionProposal, RiskDecision]:
        """自动允许的提案当场执行；其余原样返回，等待人工审批。"""
        if approval is None:
            return proposal, decision
        await self._auto_execute(proposal, decision, approval)
        refreshed = await self._store.get_proposal(proposal.id)
        return (refreshed or proposal), decision

    async def _auto_execute(
        self, proposal: ActionProposal, decision: RiskDecision, approval: Approval
    ) -> None:
        """授权已与提案一起落库；这里只记审计并走同一执行链。"""
        await self._audit_safe(
            proposal.id,
            approval.actor,
            "action.auto_approved",
            {
                "approval_id": approval.id,
                "policy_version": decision.policy_version,
                "reasons": decision.reasons,
            },
        )
        try:
            await self._execute(proposal, approval)
        except ActionError as exc:
            # 提案已持久化且授权仍有效，人工可用 resume 继续；错误交给调用方。
            await self._audit_safe(
                proposal.id, "action-executor", "action.auto_blocked", {"code": exc.code}
            )
            raise AutoExecutionError(exc.code, exc.message, proposal.id) from exc
        except Exception as exc:  # noqa: BLE001 - 已落库的提案必须有可追踪的失败上下文
            await self._audit_safe(
                proposal.id,
                "action-executor",
                "action.auto_blocked",
                {"code": exc.__class__.__name__},
            )
            raise AutoExecutionError(
                "auto_execute_error", exc.__class__.__name__, proposal.id
            ) from exc

    async def _audit_safe(
        self, proposal_id: str, actor: str, event: str, payload: dict[str, Any]
    ) -> None:
        """审计失败不能把已经落库的提案变成“不知道有没有提案”。"""
        try:
            await self._audit(proposal_id, actor, event, payload)
        except Exception as exc:  # noqa: BLE001 - 保留提案 ID 比丢审计更重要
            logger.warning("自动执行审计写入失败：{}", exc)

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
            if proposal.kind is ActionKind.RESTART_POD:
                return await self._execute_restart(proposal, approval)
            return await self._execute_rollout(proposal, approval)
        finally:
            await self._store.release_lock(key, proposal.id)

    async def _execute_rollout(self, proposal: ActionProposal, approval: Approval) -> ActionAttempt:
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

    async def _execute_restart(self, proposal: ActionProposal, approval: Approval) -> ActionAttempt:
        adapter = self._restart_adapter
        if adapter is None:
            raise await self._fail(proposal, "adapter_missing", "未配置 restart_pod 适配器")
        try:
            plan = await asyncio.to_thread(
                adapter.plan, proposal.target.namespace, proposal.target.name
            )
        except ActionError as exc:
            raise await self._fail(proposal, exc.code, exc.message) from exc
        # Pod 的 resourceVersion 随状态更新频繁变化，身份以 UID 为准。
        if plan.uid != proposal.target.uid:
            raise await self._fail(proposal, "drift", "审批后目标 Pod 已变化，需重新提案")
        if plan.owner_kind != "ReplicaSet" or not plan.owner_uid:
            raise await self._fail(
                proposal, "not_controlled", "目标 Pod 没有 ReplicaSet 归属，删除后不会重建"
            )
        if plan.template_hash != proposal.parameters.get("template_hash"):
            raise await self._fail(proposal, "template_changed", "目标 Pod 的模板与提案时不同")
        # 用执行前的真实状态重跑一次规则：提案等待期间目标可能失去可丢弃标记，
        # 持久化的提案本身也可能被改成不满足硬约束。
        rechecked = proposal.model_copy(
            update={
                "parameters": {
                    **proposal.parameters,
                    "owner_kind": plan.owner_kind,
                    "discardable": plan.discardable,
                    "template_hash": plan.template_hash,
                }
            }
        )
        workload = str(
            proposal.parameters.get("workload") or plan.workload_uid or plan.owner_uid or plan.name
        )
        prior_failure = await self._store.latest_proposal_blocks_auto(
            proposal.target.cluster,
            plan.namespace,
            workload,
            plan.owner_name or "",
            exclude_proposal=proposal.id,
        )
        if (
            decide(rechecked, target_busy=False, prior_auto_failure=prior_failure).disposition
            is RiskDisposition.DENY
        ):
            raise await self._fail(proposal, "policy_denied", "执行前规则复查拒绝，不能删除该 Pod")
        if prior_failure and approval.actor.startswith("policy:"):
            raise await self._fail(
                proposal,
                "auto_stopped",
                "同一工作负载最近一次执行未成功，自动授权失效，需人工重新提案",
            )

        # Service 是删除后唯一的业务验证手段；删除前先确认选择关系成立（含 NotReady）。
        service = str(proposal.parameters.get("service") or "")
        endpoints = await asyncio.to_thread(
            adapter.service_selects, plan.namespace, service, plan.ip, ready_only=False
        )
        if endpoints.get("ok") is not True:
            raise await self._fail(
                proposal,
                "service_not_selecting",
                "验证 Service 未选中目标 Pod，删除前停止",
            )

        attempt = ActionAttempt(
            id=f"att-{uuid.uuid4().hex[:12]}",
            proposal_id=proposal.id,
            status=AttemptStatus.INTENT,
            idempotency_key=proposal.idempotency_key,
            snapshot={
                "uid": plan.uid,
                "resource_version": plan.resource_version,
                "pod": plan.name,
                "replica_set": plan.owner_name,
                "template_hash": plan.template_hash,
                "spec_hash": plan.spec_hash,
                "restart_count": plan.restart_count,
                "approval_hash": approval.proposal_hash,
            },
            request={
                "kind": "restart_pod",
                "namespace": plan.namespace,
                "pod": plan.name,
                "service": proposal.parameters.get("service"),
                "path": proposal.parameters.get("path", "/"),
            },
            started_at=datetime.now(UTC),
        )
        try:
            await self._store.save_attempt(attempt)
        except DuplicateAttempt as exc:
            raise ActionError("already_executed", str(exc)) from exc
        await self._store.set_proposal_status(proposal.id, ProposalStatus.EXECUTING)

        try:
            await asyncio.to_thread(adapter.dry_run, plan)
        except ActionError as exc:
            return await self._finish(
                attempt,
                AttemptStatus.FAILED,
                ProposalStatus.FAILED,
                result={"stage": "dry_run", "code": exc.code, "message": exc.message},
            )

        await self._store.update_attempt(
            attempt.id, status=AttemptStatus.EXECUTING, result={"stage": "apply_started"}
        )
        try:
            await asyncio.to_thread(adapter.execute, plan)
        except Exception as exc:  # noqa: BLE001 - 删除结果未知时停止，不重放
            return await self._finish(
                attempt,
                AttemptStatus.UNKNOWN,
                ProposalStatus.UNKNOWN,
                result={
                    "stage": "apply",
                    "error": exc.__class__.__name__,
                    "code": getattr(exc, "code", None),
                    "message": str(exc)[:200],
                },
            )

        # 删除已经发出，之后的任何异常都必须落成 unknown，不能把执行记录留在 executing。
        try:
            new_pod = await asyncio.to_thread(adapter.wait_rebuilt, plan, self._rollout_timeout)
            verification = await self._verify_restart(proposal, plan, new_pod)
            live = None
            if new_pod is not None:
                live = await asyncio.to_thread(
                    adapter.pod_state, plan.namespace, plan.owner_uid or "", str(new_pod["uid"])
                )
        except Exception as exc:  # noqa: BLE001 - 删除后结果不可核实，停止等待人工
            return await self._finish(
                attempt,
                AttemptStatus.UNKNOWN,
                ProposalStatus.UNKNOWN,
                result={
                    "stage": "verify_after_delete",
                    "error": exc.__class__.__name__,
                    "code": getattr(exc, "code", None),
                    "message": str(exc)[:200],
                },
            )
        template_unchanged = bool(live) and live.get("template_hash") == plan.spec_hash
        ready = bool(live and live.get("ready"))
        restarts = int(live.get("restarts") or 0) if live else None
        ok = (
            new_pod is not None
            and verification.get("ok") is True
            and verification.get("service_selects_new_pod") is True
            and template_unchanged
            and ready
            and restarts == 0
        )
        verification = {
            **verification,
            "old_uid": plan.uid,
            "new_uid": new_pod.get("uid") if new_pod else None,
            "new_pod": new_pod.get("name") if new_pod else None,
            "template_unchanged": template_unchanged,
            "pod_ready": ready,
            "pod_restarts": restarts,
        }
        return await self._finish(
            attempt,
            AttemptStatus.VERIFIED if ok else AttemptStatus.FAILED,
            ProposalStatus.SUCCEEDED if ok else ProposalStatus.FAILED,
            result={
                "stage": "rebuilt",
                "old_uid": plan.uid,
                "new_uid": new_pod.get("uid") if new_pod else None,
            },
            verification=verification,
        )

    async def _verify_restart(
        self, proposal: ActionProposal, plan: PodPlan, new_pod: dict[str, Any] | None
    ) -> dict[str, Any]:
        """在窗口内反复请求重建后的 Pod，只看末尾连续样本，并确认 Service 已接上它。"""
        adapter = self._restart_adapter
        if adapter is None:
            return {"ok": False, "reason": "adapter_missing"}
        if new_pod is None:
            return {"ok": False, "reason": "no_new_pod", "service_selects_new_pod": False}
        service = str(proposal.parameters.get("service") or "")
        path = str(proposal.parameters.get("path") or "/")
        await asyncio.sleep(min(5.0, self._verify_grace))
        samples: list[dict[str, Any]] = []
        deadline = time.monotonic() + self._verify_seconds
        while time.monotonic() < deadline:
            samples.append(
                await asyncio.to_thread(adapter.pod_get, plan.namespace, str(new_pod["name"]), path)
            )
            await asyncio.sleep(self._sample_seconds)
        endpoints = await asyncio.to_thread(
            adapter.service_selects, plan.namespace, service, str(new_pod.get("ip") or "")
        )
        if len(samples) < self._min_samples:
            return {
                "ok": False,
                "reason": "insufficient_data",
                "samples": len(samples),
                "service_selects_new_pod": endpoints.get("ok"),
            }
        failures = [item for item in samples if not (item["ok"] and item["status"] == 200)]
        tail_failures = sum(
            1
            for item in samples[-self._min_samples :]
            if not (item["ok"] and item["status"] == 200)
        )
        return {
            "ok": tail_failures == 0,
            "samples": len(samples),
            "failed_samples": len(failures),
            "tail_failures": tail_failures,
            "window_seconds": self._verify_seconds,
            "service": service,
            "path": path,
            "service_selects_new_pod": endpoints.get("ok"),
            "service_addresses": endpoints.get("addresses"),
        }

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
