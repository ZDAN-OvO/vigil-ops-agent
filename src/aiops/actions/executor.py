"""受控执行器：提案 → 审批 → 再次检查 → 执行 → 业务验证。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from aiops.actions.outcome import SampleSummary, classify_outcome
from aiops.actions.policy import decide, target_key
from aiops.actions.restart_pod import PodPlan, PodRestartAdapter
from aiops.actions.rollout_undo import ActionError, RolloutUndoAdapter
from aiops.contracts import (
    ActionAttempt,
    ActionFollowup,
    ActionKind,
    ActionOrigin,
    ActionProposal,
    ActionPurpose,
    ActionTarget,
    Approval,
    ApprovalDecision,
    AttemptOutcome,
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


# 鉴权、代理和网关错误没有拿到业务响应，不能当成业务失败样本。
NON_BUSINESS_STATUSES = frozenset({401, 403, 502, 503, 504})


def _business_response(item: dict[str, Any]) -> bool:
    status = item.get("status")
    return status is not None and status not in NON_BUSINESS_STATUSES


def _summarise_pod_samples(samples: list[dict[str, Any]]) -> tuple[SampleSummary, int]:
    valid = [item for item in samples if _business_response(item)]
    failed = [item for item in valid if not (item.get("ok") and item.get("status") == 200)]
    return SampleSummary(samples=len(valid), failed=len(failed)), len(samples) - len(valid)


FOLLOWUP_OUTCOMES = frozenset(
    {AttemptOutcome.NO_IMPROVEMENT, AttemptOutcome.WORSENED, AttemptOutcome.UNKNOWN}
)


def _followup_for(
    attempt: ActionAttempt,
    proposal: ActionProposal,
    status: AttemptStatus,
    proposal_status: ProposalStatus,
    verification: dict[str, Any] | None,
    result: dict[str, Any] | None,
    created_at: datetime,
) -> ActionFollowup | None:
    """只有异常执行结果才需要人工待办；写前失败不建执行记录也不建待办。"""
    if proposal_status is ProposalStatus.EXPIRED:
        return None
    if status not in (AttemptStatus.FAILED, AttemptStatus.UNKNOWN):
        return None
    payload = verification or result or {}
    raw = payload.get("outcome")
    try:
        outcome = AttemptOutcome(raw) if raw else None
    except ValueError:
        return None
    if outcome not in FOLLOWUP_OUTCOMES:
        return None
    reason = str(payload.get("outcome_reason") or f"执行结果为 {outcome.value}，需要人工跟进")
    return ActionFollowup(
        id=f"fol-{uuid.uuid4().hex[:12]}",
        proposal_id=proposal.id,
        attempt_id=attempt.id,
        cluster=proposal.target.cluster,
        namespace=proposal.target.namespace,
        target_kind=proposal.target.kind,
        target_name=proposal.target.name,
        outcome=outcome,
        reason=reason,
        evidence=payload,
        created_at=created_at,
    )


def _unknown_verification(baseline: SampleSummary, reason: str) -> dict[str, Any]:
    empty = SampleSummary(samples=0, failed=0)
    return {
        "ok": False,
        "outcome": AttemptOutcome.UNKNOWN.value,
        "outcome_reason": reason,
        "baseline": baseline.to_payload(),
        "post": empty.to_payload(),
        "post_tail": empty.to_payload(),
    }


def _probe_points(points: list[list[Any]]) -> tuple[list[list[Any]], int]:
    """0 表示没有响应；鉴权与网关错误与重启路径一样不计入业务样本。"""
    valid: list[list[Any]] = []
    for point in points:
        try:
            value = float(point[1])
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value != 0.0 and int(value) not in NON_BUSINESS_STATUSES:
            valid.append(point)
    return valid, len(points) - len(valid)


def _probe_ok(point: list[Any]) -> bool:
    return 200 <= float(point[1]) < 300


def _execution_status(
    outcome: AttemptOutcome, *, samples: int, min_samples: int, known_failure: bool = False
) -> tuple[AttemptStatus, ProposalStatus]:
    """只有“连结果都没采到”才记 unknown；资源检查失败是确定的失败。"""
    if outcome is AttemptOutcome.IMPROVED:
        return AttemptStatus.VERIFIED, ProposalStatus.SUCCEEDED
    if outcome is AttemptOutcome.UNKNOWN and not known_failure and samples < min_samples:
        return AttemptStatus.UNKNOWN, ProposalStatus.UNKNOWN
    return AttemptStatus.FAILED, ProposalStatus.FAILED


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


def _min_samples_for(verify_seconds: float, sample_seconds: float) -> int:
    """后段取 2–3 个样本；135 秒窗口用 3 个，避免单个预热失败否定恢复。"""
    samples = int(verify_seconds // max(1, sample_seconds))
    return max(2, min(3, samples // 2))


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
        probe_expression: str = 'probe_http_status_code{job="frontend-probe"}',
        io_timeout_seconds: float = 10.0,
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
        self._io_timeout = io_timeout_seconds
        self._min_samples = _min_samples_for(verify_seconds, sample_seconds)

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
        purpose: ActionPurpose = ActionPurpose.REMEDIATION,
        hypothesis_id: str | None = None,
        prediction: str = "",
        absence_meaning: str = "",
        business_target: str = "",
        observation_window_seconds: int | None = None,
    ) -> tuple[ActionProposal, RiskDecision]:
        """旧入口：准备 + 自动允许时当场执行；保持 act/resume 历史行为。"""
        proposal, decision, approval = await self.prepare_rollback(
            deployment=deployment,
            reason=reason,
            actor=actor,
            namespace=namespace,
            investigation_id=investigation_id,
            origin=origin,
            evidence_ids=evidence_ids,
            purpose=purpose,
            hypothesis_id=hypothesis_id,
            prediction=prediction,
            absence_meaning=absence_meaning,
            business_target=business_target,
            observation_window_seconds=observation_window_seconds,
        )
        return await self._after_decision(proposal, decision, approval)

    async def prepare_rollback(
        self,
        *,
        deployment: str,
        reason: str,
        actor: str,
        namespace: str | None = None,
        investigation_id: str | None = None,
        origin: ActionOrigin = ActionOrigin.OPERATOR,
        evidence_ids: list[str] | None = None,
        purpose: ActionPurpose = ActionPurpose.REMEDIATION,
        hypothesis_id: str | None = None,
        prediction: str = "",
        absence_meaning: str = "",
        business_target: str = "",
        observation_window_seconds: int | None = None,
    ) -> tuple[ActionProposal, RiskDecision, Approval | None]:
        """只做读检查与提案落库；不写集群，自动授权也只落库不执行。"""
        namespace = namespace or self._namespace
        window = self._purpose_window(
            purpose, prediction, absence_meaning, business_target, observation_window_seconds
        )
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
            purpose=purpose,
            hypothesis_id=hypothesis_id,
            prediction=prediction,
            observation_conditions={
                "expected_change": prediction,
                "absence_meaning": absence_meaning,
                "business_target": business_target,
                "window_seconds": window,
            },
            observation_window_seconds=window,
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
                "purpose": purpose.value,
                "hypothesis_id": hypothesis_id,
            },
        )
        return proposal, decision, approval

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
        purpose: ActionPurpose = ActionPurpose.REMEDIATION,
        hypothesis_id: str | None = None,
        prediction: str = "",
        absence_meaning: str = "",
        business_target: str = "",
        observation_window_seconds: int | None = None,
    ) -> tuple[ActionProposal, RiskDecision]:
        """旧入口：准备 + 自动允许时当场执行；保持 act 命令历史行为。"""
        proposal, decision, approval = await self.prepare_restart_pod(
            pod=pod,
            service=service,
            reason=reason,
            actor=actor,
            path=path,
            namespace=namespace,
            investigation_id=investigation_id,
            origin=origin,
            evidence_ids=evidence_ids,
            purpose=purpose,
            hypothesis_id=hypothesis_id,
            prediction=prediction,
            absence_meaning=absence_meaning,
            business_target=business_target,
            observation_window_seconds=observation_window_seconds,
        )
        return await self._after_decision(proposal, decision, approval)

    async def prepare_restart_pod(
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
        purpose: ActionPurpose = ActionPurpose.REMEDIATION,
        hypothesis_id: str | None = None,
        prediction: str = "",
        absence_meaning: str = "",
        business_target: str = "",
        observation_window_seconds: int | None = None,
    ) -> tuple[ActionProposal, RiskDecision, Approval | None]:
        """读真实目标、验证适用性并落提案；不写集群。"""
        namespace = namespace or self._namespace
        window = self._purpose_window(
            purpose, prediction, absence_meaning, business_target, observation_window_seconds
        )
        adapter = self._restart_adapter
        if adapter is None:
            raise ActionError("adapter_missing", "未配置 restart_pod 适配器")
        plan = await asyncio.to_thread(adapter.plan, namespace, pod)
        applicability = await self._restart_applicability(plan, service)
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
                "applicability": applicability,
            },
            idempotency_key=f"restart:{plan.uid}",
            reason=reason,
            origin=origin,
            investigation_id=investigation_id,
            evidence_ids=evidence_ids or [],
            created_at=datetime.now(UTC),
            purpose=purpose,
            hypothesis_id=hypothesis_id,
            prediction=prediction,
            observation_conditions={
                "expected_change": prediction,
                "absence_meaning": absence_meaning,
                "business_target": business_target or f"{service}{path}",
                "window_seconds": window,
            },
            observation_window_seconds=window,
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
                "purpose": purpose.value,
                "hypothesis_id": hypothesis_id,
            },
        )
        return proposal, decision, approval

    def _purpose_window(
        self,
        purpose: ActionPurpose,
        prediction: str,
        absence_meaning: str,
        business_target: str,
        observation_window_seconds: int | None,
    ) -> int:
        """诊断动作必须说明预测、缺失含义和业务验证目标；窗口只能取配置值。"""
        window = self._verify_seconds
        requested = int(observation_window_seconds or 0)
        if 0 < requested < window:
            # 模型不能自行缩短观察窗口；更长只是请求，实际仍按配置执行。
            raise ActionError(
                "invalid_window",
                f"观察窗口由执行器配置决定，不能缩短到 {requested} 秒（配置 {window} 秒）",
            )
        if purpose is ActionPurpose.DIAGNOSTIC:
            missing = [
                name
                for name, value in (
                    ("prediction", prediction),
                    ("absence_meaning", absence_meaning),
                    ("business_target", business_target),
                )
                if not value.strip()
            ]
            if missing:
                raise ActionError(
                    "diagnostic_incomplete", "诊断动作缺少必要说明：" + "、".join(missing)
                )
        return window

    async def _restart_applicability(self, plan: PodPlan, service: str) -> dict[str, Any]:
        """删除前用真实状态回答“重建能不能区分或缓解这个故障”。

        - 没有控制器、不可丢弃、Service 未选中：没有重建前提；
        - 容器持续 OOM 且模板规格不足：重建沿用同一模板，不适用；
        - 没有 Deployment 历史可核对时，只能用当前模板与 OOM 事实判断。
        """
        adapter = self._restart_adapter
        if adapter is None:
            raise ActionError("adapter_missing", "未配置 restart_pod 适配器")
        if plan.owner_kind != "ReplicaSet" or not plan.owner_uid:
            raise ActionError("not_controlled", "目标 Pod 没有 ReplicaSet 归属，删除后不会重建")
        if not plan.discardable:
            raise ActionError("not_discardable", "目标 Pod 未标记为可丢弃工作负载")
        if not service.strip():
            raise ActionError("missing_service", "缺少用于验证恢复的 Service")
        endpoints = await asyncio.to_thread(
            adapter.service_selects, plan.namespace, service, plan.ip, ready_only=False
        )
        if endpoints.get("ok") is not True:
            raise ActionError(
                "service_not_selecting", "验证 Service 未选中目标 Pod，不能用于恢复判定"
            )
        live = await asyncio.to_thread(adapter.applicability, plan.namespace, plan.name)
        memory_limit = live.get("memory_limit")
        last_reason = live.get("last_terminated_reason")
        restarts = int(live.get("restart_count") or 0)
        previous_limit = None
        if plan.workload_name:
            try:
                previous = await asyncio.to_thread(
                    self._adapter.plan, plan.namespace, plan.workload_name
                )
                previous_limit = previous.target_memory_limit
            except Exception as exc:  # noqa: BLE001 - 读不到历史模板不等于适用
                logger.info("读取历史模板失败，按无历史规格处理：{}", exc.__class__.__name__)
                previous_limit = None
        spec_insufficient = (
            previous_limit is not None
            and memory_limit is not None
            and previous_limit != memory_limit
            and last_reason == "OOMKilled"
        )
        sustained_oom = last_reason == "OOMKilled" and restarts >= 2
        if spec_insufficient or sustained_oom:
            raise ActionError(
                "oom_template_inapplicable",
                "容器持续 OOM 且当前模板内存规格不足，重建 Pod 会沿用同一模板，不适用",
            )
        return {
            "checked": True,
            "memory_limit": memory_limit,
            "previous_template_memory_limit": previous_limit,
            "last_terminated_reason": last_reason,
            "restart_count": restarts,
            "service_selects_pod": True,
            "pod_ready": live.get("ready"),
            "image": live.get("image"),
        }

    def adapter_upper_bound(self) -> float:
        """动作适配器的最坏时间上界，用于发写请求前的预算预留。

        按现有基线采样、收敛、缓冲、验证窗口和 I/O 超时求和，不用模型估计。
        """
        io = self._io_timeout
        baseline = max(0, self._min_samples - 1) * self._sample_seconds
        restart = (
            baseline
            + self._rollout_timeout
            + min(5.0, self._verify_grace)
            + self._verify_seconds
            + 6 * io
        )
        rollout = (
            self._rollout_timeout
            + self._verify_grace
            + self._verify_seconds
            + 6 * io
            + 2 * self._sample_seconds
        )
        return float(max(restart, rollout))

    async def execute_authorized(
        self, proposal_id: str, *, actor: str = "operator"
    ) -> ActionAttempt:
        """执行已授权提案；重新校验授权、有效期、提案哈希与目标。"""
        proposal = await self._store.get_proposal(proposal_id)
        if proposal is None:
            raise ActionError("not_found", f"提案不存在：{proposal_id}")
        if await self._store.get_attempt(proposal_id) is not None:
            raise ActionError("already_executed", "提案已有执行记录，不重复写")
        approval = await self._store.get_approval(proposal_id)
        if approval is None or approval.decision is not ApprovalDecision.APPROVED:
            raise ActionError("no_approval", "找不到有效的批准记录，不能执行")
        if proposal.status is not ProposalStatus.APPROVED:
            raise ActionError("not_approved", f"提案状态为 {proposal.status.value}，不能继续执行")
        await self._audit(
            proposal.id,
            actor,
            "action.execution_requested",
            {"approval_id": approval.id},
        )
        return await self._execute(proposal, approval)

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

    async def abandon(self, proposal_id: str, *, actor: str, reason: str) -> ActionProposal:
        """把未执行的提案标记为拒绝，释放同一目标版本的幂等键。

        用于审批后预算或前提不再满足、任务已终止的情形：没有写请求，也不应
        继续占用 `idempotency_key` 阻止新的提案。
        """
        proposal = await self._store.get_proposal(proposal_id)
        if proposal is None:
            raise ActionError("not_found", f"提案不存在：{proposal_id}")
        if await self._store.get_attempt(proposal_id) is not None:
            raise ActionError("already_executed", "提案已有执行记录，不能放弃")
        if proposal.status in {ProposalStatus.REJECTED, ProposalStatus.EXPIRED}:
            return proposal
        await self._store.set_proposal_status(proposal_id, ProposalStatus.REJECTED)
        await self._audit(
            proposal_id,
            actor,
            "action.abandoned",
            {"reason": reason, "previous_status": proposal.status.value},
        )
        saved = await self._store.get_proposal(proposal_id)
        if saved is None:
            raise LookupError(f"提案不存在：{proposal_id}")
        return saved

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

        # 没有动作前基线就无法判断执行后是没改善还是变差，必须在写操作之前拿到。
        try:
            baseline = await self._business_baseline()
        except ActionError as exc:
            raise await self._fail(proposal, exc.code, exc.message) from exc

        # 基线窗口很长，期间目标可能漂移或授权过期，写前必须再核对一次。
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
            raise await self._fail(proposal, "drift", "基线期间目标资源已变化，需重新提案")
        await self._require_approval_valid(proposal, approval)

        # dry-run 不是写操作；通过后再用最终状态核对一次，才落执行意图。
        try:
            await asyncio.to_thread(
                self._adapter.apply, plan, dry_run=True, proposal_id=proposal.id
            )
        except ActionError as exc:
            raise await self._fail(proposal, exc.code, exc.message) from exc
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
            raise await self._fail(proposal, "drift", "dry-run 后目标资源已变化，需重新提案")
        await self._require_approval_valid(proposal, approval)

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
                "baseline": baseline.to_payload(),
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
        await self._store.update_attempt(
            attempt.id,
            status=AttemptStatus.EXECUTING,
            result={"stage": "apply_started"},
        )
        # 最后一次校验放在所有数据库 I/O 之后、真正写集群之前。
        if approval.expires_at is not None and datetime.now(UTC) > approval.expires_at:
            return await self._finish(
                attempt,
                AttemptStatus.FAILED,
                ProposalStatus.EXPIRED,
                result={"code": "expired"},
                verification=_unknown_verification(baseline, "审批在写入前过期，未写集群"),
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
                verification=_unknown_verification(baseline, "写请求结果未知，停止等待人工核实"),
            )

        try:
            rolled_out = await asyncio.to_thread(
                self._adapter.wait_rolled_out, plan, self._rollout_timeout
            )
            # 滚动完成后再等两个抓取周期，避免把恢复前的最后一个样本算进来。
            await asyncio.sleep(self._verify_grace)
            recovered_from = datetime.now(UTC)
            await asyncio.sleep(self._verify_delay)
            verification = await self._verify_business(recovered_from)
            runtime = await self._runtime_state(proposal)
        except Exception as exc:  # noqa: BLE001 - 应用后无法核实，停止等待人工
            return await self._finish(
                attempt,
                AttemptStatus.UNKNOWN,
                ProposalStatus.UNKNOWN,
                result={"stage": "verify_after_apply", "error": exc.__class__.__name__},
                verification=_unknown_verification(
                    baseline, "应用后无法核实业务结果，停止等待人工"
                ),
            )
        ready = [pod for pod in runtime.get("pods", []) if pod.get("ready")]
        restarts = sum(int(pod.get("restarts") or 0) for pod in ready)
        # 无内存限额目标时跳过该检查；有目标就必须在窗口内回到目标规格且无重启。
        limit_ok = (
            plan.target_memory_limit is None
            or runtime.get("memory_limit") == plan.target_memory_limit
        )
        post_full = SampleSummary(
            samples=int(verification.get("samples") or 0),
            failed=int(verification.get("failed_samples") or 0),
        )
        # 与恢复判定使用同一个末段窗口，避免预热失败单独触发恶化。
        post = SampleSummary(
            samples=int(verification.get("tail_samples") or 0),
            failed=int(verification.get("tail_failed_samples") or 0),
        )
        resource_ok = rolled_out and limit_ok and bool(ready) and restarts == 0
        post_unavailable = int(verification.get("tail_unavailable_samples") or 0)
        decision = classify_outcome(
            baseline,
            post,
            business_ok=verification.get("ok") is True,
            resource_ok=resource_ok,
            post_reliable=post.samples >= self._min_samples and post_unavailable == 0,
        )
        verification = {
            **verification,
            "runtime": runtime,
            "memory_limit_restored": limit_ok,
            "ready_pods": len(ready),
            "pod_restarts": restarts,
            "outcome": decision.outcome.value,
            "outcome_reason": decision.reason,
            "baseline": decision.baseline.to_payload(),
            "post": post_full.to_payload(),
            "post_tail": decision.post.to_payload(),
        }
        attempt_status, proposal_status = _execution_status(
            decision.outcome,
            samples=post.samples,
            min_samples=self._min_samples,
            known_failure=not resource_ok,
        )
        return await self._finish(
            attempt,
            attempt_status,
            proposal_status,
            result={"stage": "applied", "rolled_out": rolled_out},
            verification=verification,
        )

    async def _execute_restart(self, proposal: ActionProposal, approval: Approval) -> ActionAttempt:
        adapter = self._restart_adapter
        if adapter is None:
            raise await self._fail(proposal, "adapter_missing", "未配置 restart_pod 适配器")
        plan = await self._restart_preconditions(proposal, approval)

        # 删除后旧 Pod 不再存在，基线只能在删除前采集。
        try:
            baseline = await self._restart_baseline(
                plan, str(proposal.parameters.get("path") or "/")
            )
        except ActionError as exc:
            raise await self._fail(proposal, exc.code, exc.message) from exc

        # 采样等待期间目标可能失去可丢弃标记、Service 选择关系、停止门状态或授权有效期。
        plan = await self._restart_preconditions(proposal, approval)
        await self._require_approval_valid(proposal, approval)

        # dry-run 不是写操作；通过后还要用最终状态再核对一次，再落执行意图。
        try:
            await asyncio.to_thread(adapter.dry_run, plan)
        except ActionError as exc:
            raise await self._fail(proposal, exc.code, exc.message) from exc
        plan = await self._restart_preconditions(proposal, approval)
        await self._require_approval_valid(proposal, approval)

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
                "baseline": baseline.to_payload(),
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
        await self._store.update_attempt(
            attempt.id, status=AttemptStatus.EXECUTING, result={"stage": "apply_started"}
        )
        # 最后一次校验放在所有数据库 I/O 之后、真正写集群之前。
        if approval.expires_at is not None and datetime.now(UTC) > approval.expires_at:
            return await self._finish(
                attempt,
                AttemptStatus.FAILED,
                ProposalStatus.EXPIRED,
                result={"code": "expired"},
                verification=_unknown_verification(baseline, "审批在写入前过期，未写集群"),
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
                verification=_unknown_verification(baseline, "删除结果未知，停止等待人工核实"),
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
                verification=_unknown_verification(
                    baseline, "删除后无法核实业务结果，停止等待人工"
                ),
            )
        template_unchanged = bool(live) and live.get("template_hash") == plan.spec_hash
        ready = bool(live and live.get("ready"))
        restarts = int(live.get("restarts") or 0) if live else None
        post_full = SampleSummary(
            samples=int(verification.get("samples") or 0),
            failed=int(verification.get("failed_samples") or 0),
        )
        # 与恢复判定使用同一个末段窗口，避免预热失败单独触发恶化。
        post = SampleSummary(
            samples=int(verification.get("tail_samples") or 0),
            failed=int(verification.get("tail_failed_samples") or 0),
        )
        resource_ok = (
            new_pod is not None
            and verification.get("service_selects_new_pod") is True
            and template_unchanged
            and ready
            and restarts == 0
        )
        known_failure = bool(verification.get("known_failure"))
        post_unavailable = int(verification.get("tail_unavailable_samples") or 0)
        decision = classify_outcome(
            baseline,
            post,
            business_ok=verification.get("ok") is True,
            resource_ok=resource_ok,
            known_failure=known_failure,
            post_reliable=(post.samples >= self._min_samples and post_unavailable == 0)
            or known_failure,
        )
        verification = {
            **verification,
            "old_uid": plan.uid,
            "new_uid": new_pod.get("uid") if new_pod else None,
            "new_pod": new_pod.get("name") if new_pod else None,
            "template_unchanged": template_unchanged,
            "pod_ready": ready,
            "pod_restarts": restarts,
            "outcome": decision.outcome.value,
            "outcome_reason": decision.reason,
            "baseline": decision.baseline.to_payload(),
            "post": post_full.to_payload(),
            "post_tail": decision.post.to_payload(),
        }
        attempt_status, proposal_status = _execution_status(
            decision.outcome,
            samples=post.samples,
            min_samples=self._min_samples,
            known_failure=known_failure or not resource_ok,
        )
        return await self._finish(
            attempt,
            attempt_status,
            proposal_status,
            result={
                "stage": "rebuilt",
                "old_uid": plan.uid,
                "new_uid": new_pod.get("uid") if new_pod else None,
            },
            verification=verification,
        )

    async def _restart_preconditions(self, proposal: ActionProposal, approval: Approval) -> PodPlan:
        """删除前的全部硬检查；基线等待前后各跑一次，后一次用最终状态。"""
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
            logger.warning(
                "Service 未选中目标 Pod：service={} pod={} ip={} endpoints={}",
                service,
                plan.name,
                plan.ip,
                endpoints,
            )
            raise await self._fail(
                proposal,
                "service_not_selecting",
                "验证 Service 未选中目标 Pod，删除前停止",
            )
        return plan

    async def _require_approval_valid(self, proposal: ActionProposal, approval: Approval) -> None:
        if approval.expires_at is not None and datetime.now(UTC) > approval.expires_at:
            raise await self._fail(
                proposal, "expired", "审批已过期，需重新提案", ProposalStatus.EXPIRED
            )

    async def _restart_baseline(self, plan: PodPlan, path: str) -> SampleSummary:
        """删除前直接从目标 Pod 采样；连接级失败不能当作基线。"""
        adapter = self._restart_adapter
        if adapter is None:
            raise ActionError("adapter_missing", "未配置 restart_pod 适配器")
        samples: list[dict[str, Any]] = []
        for index in range(self._min_samples):
            samples.append(
                await asyncio.to_thread(adapter.pod_get, plan.namespace, plan.name, path)
            )
            if index + 1 < self._min_samples:
                await asyncio.sleep(self._sample_seconds)
        summary, unavailable = _summarise_pod_samples(samples)
        if unavailable:
            raise ActionError(
                "baseline_unavailable",
                f"动作前有 {unavailable} 次请求没有取得业务响应，停止",
            )
        return summary

    async def _business_baseline(self) -> SampleSummary:
        end = datetime.now(UTC)
        start = end - timedelta(seconds=self._verify_seconds)
        result = await self._query_probe(start, end)
        if result.error is not None:
            raise ActionError("baseline_unavailable", f"动作前查询失败：{result.error.code}")
        points = [
            point
            for series in (result.data or {}).get("series", [])
            for point in series.get("points", [])
        ]
        valid, unavailable = _probe_points(points)
        if len(valid) < self._min_samples:
            raise ActionError(
                "baseline_unavailable",
                f"动作前有效样本不足（无效 {unavailable} 个），停止",
            )
        failed = sum(1 for point in valid if not _probe_ok(point))
        return SampleSummary(samples=len(valid), failed=failed)

    async def _query_probe(self, start: datetime, end: datetime):
        context = ToolContext.create(
            investigation_id=f"action-check-{self._cluster}",
            cluster=self._cluster,
            namespace=self._namespace,
            runtime_path=self._runtime_path,
            timeout_seconds=30,
        )
        return await self._prometheus.query_range(
            context,
            self._probe_expression,
            TimeRange(start=start, end=end),
            step_seconds=self._sample_seconds,
        )

    async def _verify_restart(
        self, proposal: ActionProposal, plan: PodPlan, new_pod: dict[str, Any] | None
    ) -> dict[str, Any]:
        """在窗口内反复请求重建后的 Pod，只看末尾连续样本，并确认 Service 已接上它。"""
        adapter = self._restart_adapter
        if adapter is None:
            return {"ok": False, "reason": "adapter_missing", "samples": 0, "failed_samples": 0}
        if new_pod is None:
            return {
                "ok": False,
                "reason": "no_new_pod",
                "samples": 0,
                "failed_samples": 0,
                "known_failure": True,
                "service_selects_new_pod": False,
            }
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
        summary, unavailable = _summarise_pod_samples(samples)
        raw_tail = samples[-self._min_samples :]
        tail = [item for item in raw_tail if _business_response(item)]
        tail_unavailable = len(raw_tail) - len(tail)
        if summary.samples < self._min_samples:
            return {
                "ok": False,
                "reason": "insufficient_data",
                "samples": summary.samples,
                "failed_samples": summary.failed,
                "unavailable_samples": unavailable,
                "tail_samples": 0,
                "tail_failed_samples": 0,
                "tail_unavailable_samples": tail_unavailable,
                "service_selects_new_pod": endpoints.get("ok"),
            }
        tail_failures = sum(1 for item in tail if not (item["ok"] and item["status"] == 200))
        return {
            "ok": tail_failures == 0,
            "samples": summary.samples,
            "failed_samples": summary.failed,
            "tail_failures": tail_failures,
            "tail_samples": len(tail),
            "tail_failed_samples": tail_failures,
            "tail_unavailable_samples": tail_unavailable,
            "unavailable_samples": unavailable,
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
        result = await self._query_probe(start, end)
        if result.error is not None:
            return {
                "ok": False,
                "reason": "query_failed",
                "error": result.error.code,
                "samples": 0,
                "failed_samples": 0,
            }
        points = [
            point
            for series in (result.data or {}).get("series", [])
            for point in series.get("points", [])
        ]
        valid, unavailable = _probe_points(points)
        failed = [point for point in valid if not _probe_ok(point)]
        raw_tail = points[-self._min_samples :]
        tail, tail_unavailable = _probe_points(raw_tail)
        if len(valid) < self._min_samples:
            return {
                "ok": False,
                "reason": "insufficient_data",
                "samples": len(valid),
                "failed_samples": len(failed),
                "unavailable_samples": unavailable,
                "tail_samples": 0,
                "tail_failed_samples": 0,
                "tail_unavailable_samples": tail_unavailable,
            }
        # 恢复判定看窗口末段的连续样本；滚动刚结束时的预热失败单独记录，不算恢复失败。
        tail_failures = sum(1 for point in tail if not _probe_ok(point))
        return {
            "ok": tail_failures == 0,
            "samples": len(valid),
            "failed_samples": len(failed),
            "tail_failures": tail_failures,
            "tail_samples": len(tail),
            "tail_failed_samples": tail_failures,
            "tail_unavailable_samples": tail_unavailable,
            "unavailable_samples": unavailable,
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
        proposal = await self._store.get_proposal(attempt.proposal_id)
        if proposal is None:
            raise LookupError(f"提案不存在：{attempt.proposal_id}")
        followup = _followup_for(
            attempt, proposal, status, proposal_status, verification, result, finished_at
        )
        await self._store.finish_attempt(
            attempt.id,
            proposal_id=proposal.id,
            proposal_status=proposal_status,
            status=status,
            result=result,
            verification=verification,
            finished_at=finished_at,
            followup=followup,
        )
        await self._audit(
            attempt.proposal_id,
            "action-executor",
            f"action.{proposal_status.value}",
            {
                "attempt_id": attempt.id,
                "attempt_status": status.value,
                "verification": verification,
                "followup_id": followup.id if followup else None,
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
