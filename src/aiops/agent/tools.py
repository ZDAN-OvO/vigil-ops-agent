"""单 Agent 的业务工具：只读取证、假设维护、证据判定与动作请求。

模型直接调用这些工具，不再输出一个包含所有工具参数的通用请求结构。工具只从运行
上下文取得固定的集群、命名空间、预算与 Store；模型不能修改这些权限，提示词也不
包含注入真值、场景文件或实验答案。

每个工具的请求都先登记到 `tool_requests` 账本：同一 `(investigation_id,
tool_call_id)` 重放时直接返回已保存的结果，不重复计数、不重复写。
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from langchain.tools import ToolRuntime
from langchain_core.tools import tool
from langgraph.types import interrupt
from loguru import logger
from pydantic import ValidationError

from aiops.actions.executor import AutoExecutionError
from aiops.actions.rollout_undo import ActionError
from aiops.agent.budget import BudgetExhausted, WriteReserveUnavailable
from aiops.agent.context import AgentContext
from aiops.agent.schemas import (
    AssessEvidenceArgs,
    HypothesisOperation,
    ManageHypothesesArgs,
    RequestActionArgs,
)
from aiops.contracts import (
    ActionAttempt,
    ActionOrigin,
    ActionProposal,
    ActionPurpose,
    AttemptOutcome,
    Hypothesis,
    HypothesisEvidence,
    Observation,
    ObservationStatus,
    ProposalStatus,
    RiskDecision,
    RiskDisposition,
    ToolCall,
    ToolRequest,
    ToolRequestState,
)
from aiops.investigation.toolbox import QueryRequest, observation
from aiops.tools.base import ToolContext

READ_TOOLS = (
    "prometheus_query_range",
    "kubernetes_list_deployments",
    "kubernetes_list_pods",
    "kubernetes_list_events",
    "kubernetes_list_services",
    "kubernetes_list_logs",
    "knowledge_list_runbooks",
    "knowledge_get_runbook",
)

TERMINAL_LEDGER_STATES = frozenset(
    {
        ToolRequestState.OBSERVED,
        ToolRequestState.FAILED,
        ToolRequestState.ACTION_EXECUTED,
        ToolRequestState.REJECTED,
    }
)


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _error(code: str, message: str, *, retryable: bool = False) -> str:
    return _dump(
        {
            "status": "error",
            "error": {"code": code, "message": message, "retryable": retryable},
        }
    )


def _is_empty(data: Any) -> bool:
    """空结果只表示这次没取到数据，不是反证。"""
    if data is None:
        return True
    if isinstance(data, dict):
        if "series" in data:
            series = data.get("series") or []
            return not any(series_item.get("points") for series_item in series)
        if "items" in data:
            return not (data.get("items") or [])
        if data.get("id"):
            return False
        return not data
    if isinstance(data, list):
        return not data
    return False


async def _prepare_request(
    runtime: ToolRuntime[AgentContext], tool_name: str, arguments: dict[str, Any]
) -> tuple[AgentContext, str, str | None]:
    """登记请求或返回已保存的结果；cached 非空表示这条工具调用应直接重放。"""
    ctx = runtime.context
    call_id = str(runtime.tool_call_id or "")
    if not call_id:
        return ctx, "", _error("missing_tool_call_id", "工具调用缺少 ID，不能登记请求")
    existing = await ctx.store.get_tool_request(ctx.investigation_id, call_id)
    if existing is not None:
        if existing.state is ToolRequestState.ACTION_PENDING:
            # 等待审批的请求在恢复时要走“读账本与提案”的分支，不能当作已完成重放。
            return ctx, call_id, None
        cached = None
        if isinstance(existing.result, dict):
            cached = existing.result.get("tool_message")
        if cached:
            return ctx, call_id, str(cached)
        if existing.state is ToolRequestState.RUNNING:
            attempt = (
                await _attempt_for(ctx, existing.proposal_id) if existing.proposal_id else None
            )
            if attempt is not None:
                return (
                    ctx,
                    call_id,
                    _dump(
                        {
                            "status": "in_flight",
                            "proposal_id": existing.proposal_id,
                            "attempt_id": attempt.id,
                            "attempt_status": attempt.status.value,
                            "note": (
                                "写请求可能已经发出，不能重复执行；"
                                "请读取已保存的执行记录或等人工核实。"
                            ),
                        }
                    ),
                )
            return (
                ctx,
                call_id,
                _error(
                    "request_in_flight",
                    "同一请求仍在执行或结果未知，不能重复执行；请读取已保存结果或等待人工核实",
                ),
            )
        return ctx, call_id, cached
    await ctx.store.start_tool_request(
        ToolRequest(
            investigation_id=ctx.investigation_id,
            tool_call_id=call_id,
            tool=tool_name,
            arguments=arguments,
            state=ToolRequestState.RUNNING,
            started_at=datetime.now(UTC),
        )
    )
    try:
        await ctx.budget.spend_tool_call()
    except BudgetExhausted as exc:
        message = _dump(
            {
                "status": "not_started",
                "code": "budget_exhausted",
                "message": f"工具调用预算已耗尽：{exc.item}",
                "note": "不能再取证；请立即用已有观测提交结构化报告。",
            }
        )
        await _finish_request(
            ctx,
            call_id,
            state=ToolRequestState.REJECTED,
            message=message,
            error_code="budget_exhausted",
        )
        return ctx, call_id, message
    return ctx, call_id, None


async def _finish_request(
    ctx: AgentContext,
    call_id: str,
    *,
    state: ToolRequestState,
    message: str,
    result_ref: str | None = None,
    error_code: str | None = None,
    proposal_id: str | None = None,
    action_attempt_id: str | None = None,
) -> None:
    if not call_id:
        return
    await ctx.store.finish_tool_request(
        ctx.investigation_id,
        call_id,
        state=state,
        result={"tool_message": message},
        result_ref=result_ref,
        error_code=error_code,
        proposal_id=proposal_id,
        action_attempt_id=action_attempt_id,
    )


def _tool_context(ctx: AgentContext) -> ToolContext:
    return ToolContext(
        investigation_id=ctx.investigation_id,
        cluster=ctx.cluster,
        namespace=ctx.namespace,
        runtime_path=ctx.runtime_path,
        deadline=time.monotonic()
        + ctx.budget.read_tool_timeout(configured=ctx.tool_timeout_seconds),
    )


async def _run_read(
    runtime: ToolRuntime[AgentContext], *, tool_name: str, request: QueryRequest
) -> str:
    ctx, call_id, cached = await _prepare_request(runtime, tool_name, request.model_dump())
    if cached is not None:
        return cached
    started_at = datetime.now(UTC)
    try:
        result = await ctx.toolbox.run(_tool_context(ctx), ctx.window, request)
    except Exception as exc:  # noqa: BLE001 - 工具异常不能中断整轮，但要留失败记录
        logger.warning("只读工具异常：{} {}", tool_name, exc.__class__.__name__)
        message = _error("tool_exception", exc.__class__.__name__)
        await _finish_request(
            ctx,
            call_id,
            state=ToolRequestState.FAILED,
            message=message,
            error_code="tool_exception",
        )
        return message
    finished_at = datetime.now(UTC)
    status = (
        ObservationStatus.ERROR
        if result.error is not None
        else ObservationStatus.EMPTY
        if _is_empty(result.data)
        else ObservationStatus.OK
    )
    summary = observation(result, max_chars=ctx.max_observation_chars)
    # 工具的调用 ID 由模型生成，跨调查并不保证唯一；业务表用调查前缀做主键。
    record_id = f"{ctx.investigation_id}:{call_id}"
    observation_id = f"obs-{uuid.uuid4().hex[:12]}"
    await ctx.store.save_tool_call(
        ToolCall(
            id=record_id,
            investigation_id=ctx.investigation_id,
            tool=tool_name,
            parameters=request.model_dump(),
            time_range=result.time_range,
            started_at=started_at,
            finished_at=finished_at,
            error_code=result.error.code if result.error else None,
            truncated=result.truncated,
            raw_ref=result.raw_ref,
        )
    )
    payload: dict[str, Any] = {
        "tool_call_id": call_id,
        "observation_id": observation_id if result.error is None else None,
        "tool": tool_name,
        "status": status.value,
        "observation": summary,
        "raw_ref": result.raw_ref,
        "truncated": result.truncated,
        "error": result.error.model_dump() if result.error else None,
    }
    if result.error is None:
        await ctx.store.save_observation(
            Observation(
                id=observation_id,
                investigation_id=ctx.investigation_id,
                tool_call_id=record_id,
                source=result.source,
                independence_key=f"{result.source}:{tool_name}",
                summary=summary,
                raw_ref=result.raw_ref or "",
                status=status,
                truncated=result.truncated,
                time_range=result.time_range,
            )
        )
    if status is ObservationStatus.EMPTY:
        payload["note"] = "未取到数据；空结果不是反证，不能作为 refuting 依据。"
    message = _dump(payload)
    if result.error is not None:
        await _finish_request(
            ctx,
            call_id,
            state=ToolRequestState.FAILED,
            message=message,
            error_code=result.error.code,
            result_ref=result.raw_ref,
        )
        return message
    await _finish_request(
        ctx,
        call_id,
        state=ToolRequestState.OBSERVED,
        message=message,
        result_ref=result.raw_ref,
    )
    ctx.progress.record(tool_name)
    if tool_name.startswith("knowledge."):
        for runbook_id in _runbook_ids(result.data):
            if runbook_id not in ctx.progress.runbook_ids:
                ctx.progress.runbook_ids.append(runbook_id)
    return message


def _runbook_ids(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return []
    if "items" in data:
        return [str(item.get("id")) for item in data["items"] or [] if item.get("id")]
    if data.get("id"):
        return [str(data["id"])]
    return []


def _query(
    tool: str,
    *,
    expression: str = "",
    lookback_seconds: int = 0,
    step_seconds: int = 0,
    selector: str = "",
    involved_name: str = "",
) -> QueryRequest:
    return QueryRequest(
        tool=tool,
        expression=expression,
        lookback_seconds=lookback_seconds,
        step_seconds=step_seconds,
        selector=selector,
        involved_name=involved_name,
    )


@tool
async def prometheus_query_range(
    expression: str,
    lookback_seconds: int,
    step_seconds: int,
    runtime: ToolRuntime[AgentContext],
) -> str:
    """查询 Prometheus 区间数据。需要 PromQL 表达式、回看秒数（60-3600）和步长（5-300）。"""
    return await _run_read(
        runtime,
        tool_name="prometheus.query_range",
        request=_query(
            "prometheus.query_range",
            expression=expression,
            lookback_seconds=lookback_seconds,
            step_seconds=step_seconds,
        ),
    )


@tool
async def kubernetes_list_deployments(selector: str, runtime: ToolRuntime[AgentContext]) -> str:
    """列出命名空间内 Deployment 与历史 revision；selector 是标签选择器，留空表示全部。"""
    return await _run_read(
        runtime,
        tool_name="kubernetes.list_deployments",
        request=_query("kubernetes.list_deployments", selector=selector),
    )


@tool
async def kubernetes_list_pods(selector: str, runtime: ToolRuntime[AgentContext]) -> str:
    """列出 Pod 的重启计数、终止原因、资源规格与标签；selector 留空表示全部。"""
    return await _run_read(
        runtime,
        tool_name="kubernetes.list_pods",
        request=_query("kubernetes.list_pods", selector=selector),
    )


@tool
async def kubernetes_list_events(involved_name: str, runtime: ToolRuntime[AgentContext]) -> str:
    """列出命名空间事件；involved_name 可填对象名，留空表示全部。"""
    return await _run_read(
        runtime,
        tool_name="kubernetes.list_events",
        request=_query("kubernetes.list_events", involved_name=involved_name),
    )


@tool
async def kubernetes_list_services(selector: str, runtime: ToolRuntime[AgentContext]) -> str:
    """列出 Service 的选择器、ClusterIP 和端口；selector 可填服务名或标签选择器。"""
    return await _run_read(
        runtime,
        tool_name="kubernetes.list_services",
        request=_query("kubernetes.list_services", selector=selector),
    )


@tool
async def kubernetes_list_logs(
    selector: str, pod_name: str, runtime: ToolRuntime[AgentContext]
) -> str:
    """读取最近日志；selector 选一个 Pod，pod_name 可指定单个 Pod。"""
    return await _run_read(
        runtime,
        tool_name="kubernetes.list_logs",
        request=_query("kubernetes.list_logs", selector=selector, involved_name=pod_name),
    )


@tool
async def knowledge_list_runbooks(
    query: str, service: str, runtime: ToolRuntime[AgentContext]
) -> str:
    """查询 runbook 候选：query 填关键词，service 填服务名。runbook 只是参考，不是证据。"""
    return await _run_read(
        runtime,
        tool_name="knowledge.list_runbooks",
        request=_query("knowledge.list_runbooks", expression=query, selector=service),
    )


@tool
async def knowledge_get_runbook(runbook_id: str, runtime: ToolRuntime[AgentContext]) -> str:
    """读取一条 runbook 的全文与来源；runbook 不能单独支撑根因或写操作授权。"""
    return await _run_read(
        runtime,
        tool_name="knowledge.get_runbook",
        request=_query("knowledge.get_runbook", involved_name=runbook_id),
    )


# --------------------------------------------------------------------------------------
# 假设维护
# --------------------------------------------------------------------------------------


def _hypothesis_payload(hypothesis: Hypothesis, *, temp_id: str = "") -> dict[str, Any]:
    return {
        "id": hypothesis.id,
        "temp_id": temp_id,
        "parent_id": hypothesis.parent_id,
        "statement": hypothesis.statement,
        "expected_observations": hypothesis.expected_observations,
        "refutation_criteria": hypothesis.refutation_criteria,
        "status": hypothesis.status.value,
        "revision": hypothesis.revision,
    }


async def _validate_parent(
    ctx: AgentContext,
    hypotheses: dict[str, Hypothesis],
    parent_id: str,
    *,
    owner_id: str = "",
) -> str | None:
    if not parent_id:
        return None
    if parent_id == owner_id:
        return "假设不能以自身为父节点"
    parent = hypotheses.get(parent_id)
    if parent is None:
        return f"父假设不存在或不属于本次调查：{parent_id}"
    seen: set[str] = set()
    cursor: str | None = parent_id
    while cursor:
        if cursor in seen:
            return "假设父子关系出现环"
        seen.add(cursor)
        if cursor == owner_id:
            return "假设父子关系出现环"
        current = hypotheses.get(cursor)
        cursor = current.parent_id if current else None
    return None


@tool
async def manage_hypotheses(
    operations: list[HypothesisOperation], runtime: ToolRuntime[AgentContext]
) -> str:
    """批量创建、细化或更新假设。create 可用 temp_id 建立同批引用；假设不能删除。

    每次内容或状态变更都必须写 reason；假设被反驳后可由新证据重新打开。
    """
    payload = {"operations": [item.model_dump() for item in operations]}
    ctx, call_id, cached = await _prepare_request(runtime, "manage_hypotheses", payload)
    if cached is not None:
        return cached
    try:
        args = ManageHypothesesArgs(operations=operations)
    except ValidationError as exc:
        message = _error("invalid_argument", str(exc))
        await _finish_request(
            ctx,
            call_id,
            state=ToolRequestState.FAILED,
            message=message,
            error_code="invalid_argument",
        )
        return message

    hypotheses = {item.id: item for item in await ctx.store.list_hypotheses(ctx.investigation_id)}
    temp_map: dict[str, str] = {}
    results: list[dict[str, Any]] = []

    def fail(code: str, message: str) -> str:
        return _error(code, message)

    for operation in args.operations:
        op = operation.op
        if op == "create":
            if not operation.statement.strip():
                message = fail("invalid_argument", "create 需要非空 statement")
                await _finish_request(
                    ctx,
                    call_id,
                    state=ToolRequestState.FAILED,
                    message=message,
                    error_code="invalid_argument",
                )
                return message
            parent_id = operation.parent_id.strip()
            if parent_id:
                parent_id = temp_map.get(parent_id, parent_id)
            problem = await _validate_parent(ctx, hypotheses, parent_id)
            if problem:
                message = fail("invalid_parent", problem)
                await _finish_request(
                    ctx,
                    call_id,
                    state=ToolRequestState.FAILED,
                    message=message,
                    error_code="invalid_parent",
                )
                return message
            hypothesis = Hypothesis(
                id=f"hyp-{uuid.uuid4().hex[:12]}",
                investigation_id=ctx.investigation_id,
                parent_id=parent_id or None,
                statement=operation.statement.strip(),
                expected_observations=[
                    item for item in operation.expected_observations if item.strip()
                ],
                origin="model",
                refutation_criteria=[
                    item for item in operation.refutation_criteria if item.strip()
                ],
                update_reason=operation.reason or "模型创建假设",
            )
            await ctx.store.save_hypothesis(hypothesis)
            hypotheses[hypothesis.id] = hypothesis
            if operation.temp_id:
                temp_map[operation.temp_id.strip()] = hypothesis.id
            results.append(_hypothesis_payload(hypothesis, temp_id=operation.temp_id))
            continue

        target_id = operation.hypothesis_id.strip() or temp_map.get(operation.temp_id.strip(), "")
        current = hypotheses.get(target_id)
        if current is None:
            message = fail("unknown_hypothesis", f"假设不存在或不属于本次调查：{target_id}")
            await _finish_request(
                ctx,
                call_id,
                state=ToolRequestState.FAILED,
                message=message,
                error_code="unknown_hypothesis",
            )
            return message
        if not operation.reason.strip():
            message = fail("missing_reason", f"{op} 必须给出 reason")
            await _finish_request(
                ctx,
                call_id,
                state=ToolRequestState.FAILED,
                message=message,
                error_code="missing_reason",
            )
            return message

        status = operation.status or current.status.value
        if op == "update_status" and operation.status is None:
            message = fail("invalid_argument", "update_status 需要 status")
            await _finish_request(
                ctx,
                call_id,
                state=ToolRequestState.FAILED,
                message=message,
                error_code="invalid_argument",
            )
            return message
        parent_id = operation.parent_id.strip()
        if parent_id:
            parent_id = temp_map.get(parent_id, parent_id)
            problem = await _validate_parent(ctx, hypotheses, parent_id, owner_id=current.id)
            if problem:
                message = fail("invalid_parent", problem)
                await _finish_request(
                    ctx,
                    call_id,
                    state=ToolRequestState.FAILED,
                    message=message,
                    error_code="invalid_parent",
                )
                return message
        updated = await ctx.store.update_hypothesis(
            current.id,
            statement=operation.statement.strip() or current.statement,
            expected_observations=(
                [item for item in operation.expected_observations if item.strip()]
                if operation.expected_observations
                else current.expected_observations
            ),
            refutation_criteria=(
                [item for item in operation.refutation_criteria if item.strip()]
                if operation.refutation_criteria
                else current.refutation_criteria
            ),
            status=status,
            update_reason=operation.reason.strip(),
            actor="agent",
            parent_id=parent_id or None,
            update_parent=bool(parent_id),
        )
        hypotheses[updated.id] = updated
        results.append(_hypothesis_payload(updated))
        ctx.progress.record("hypothesis")

    message = _dump(
        {
            "status": "ok",
            "hypotheses": results,
            "temp_map": temp_map,
            "note": "假设不能删除；反驳后可用 update_status 或新证据重新打开。",
        }
    )
    await _finish_request(ctx, call_id, state=ToolRequestState.OBSERVED, message=message)
    ctx.progress.record("hypothesis")
    return message


# --------------------------------------------------------------------------------------
# 证据判定
# --------------------------------------------------------------------------------------


@tool
async def assess_evidence(
    links: list[dict[str, Any]],
    hypothesis_updates: list[dict[str, Any]],
    runtime: ToolRuntime[AgentContext],
) -> str:
    """把观测关联到假设：supporting/refuting/neutral，并写依据；可同时更新假设状态。

    空结果和查询失败不能作为 supporting/refuting；runbook 只是参考，不能当证据。
    """
    payload = {"links": links, "hypothesis_updates": hypothesis_updates}
    ctx, call_id, cached = await _prepare_request(runtime, "assess_evidence", payload)
    if cached is not None:
        return cached
    try:
        args = AssessEvidenceArgs(links=links, hypothesis_updates=hypothesis_updates)
    except ValidationError as exc:
        message = _error("invalid_argument", str(exc))
        await _finish_request(
            ctx,
            call_id,
            state=ToolRequestState.FAILED,
            message=message,
            error_code="invalid_argument",
        )
        return message

    observations = {
        item.id: item for item in await ctx.store.list_observations(ctx.investigation_id)
    }
    hypotheses = {item.id: item for item in await ctx.store.list_hypotheses(ctx.investigation_id)}
    applied: list[dict[str, Any]] = []
    for link in args.links:
        observation_item = observations.get(link.observation_id)
        if observation_item is None:
            message = _error(
                "unknown_observation",
                f"观测不存在或不属于本次调查：{link.observation_id}",
            )
            await _finish_request(
                ctx,
                call_id,
                state=ToolRequestState.FAILED,
                message=message,
                error_code="unknown_observation",
            )
            return message
        if link.hypothesis_id not in hypotheses:
            message = _error(
                "unknown_hypothesis",
                f"假设不存在或不属于本次调查：{link.hypothesis_id}",
            )
            await _finish_request(
                ctx,
                call_id,
                state=ToolRequestState.FAILED,
                message=message,
                error_code="unknown_hypothesis",
            )
            return message
        if observation_item.status is not ObservationStatus.OK and link.relation != "neutral":
            message = _error(
                "not_real_evidence",
                f"观测 {link.observation_id} 的状态是 {observation_item.status.value}，"
                "只能记为 neutral；空结果或查询失败不是证据。",
            )
            await _finish_request(
                ctx,
                call_id,
                state=ToolRequestState.FAILED,
                message=message,
                error_code="not_real_evidence",
            )
            return message
        if observation_item.source == "knowledge" and link.relation != "neutral":
            message = _error(
                "knowledge_not_evidence",
                "runbook 是待核实的参考，不能作为 supporting/refuting 证据",
            )
            await _finish_request(
                ctx,
                call_id,
                state=ToolRequestState.FAILED,
                message=message,
                error_code="knowledge_not_evidence",
            )
            return message
        await ctx.store.upsert_hypothesis_evidence(
            HypothesisEvidence(
                id=f"he-{uuid.uuid4().hex[:12]}",
                investigation_id=ctx.investigation_id,
                hypothesis_id=link.hypothesis_id,
                observation_id=link.observation_id,
                relation=link.relation,
                rationale=link.rationale,
            )
        )
        applied.append(
            {
                "observation_id": link.observation_id,
                "hypothesis_id": link.hypothesis_id,
                "relation": link.relation,
            }
        )
        ctx.progress.record("evidence")

    updated_hypotheses: list[dict[str, Any]] = []
    for update in args.hypothesis_updates:
        current = hypotheses.get(update.hypothesis_id)
        if current is None:
            message = _error(
                "unknown_hypothesis",
                f"假设不存在或不属于本次调查：{update.hypothesis_id}",
            )
            await _finish_request(
                ctx,
                call_id,
                state=ToolRequestState.FAILED,
                message=message,
                error_code="unknown_hypothesis",
            )
            return message
        updated = await ctx.store.update_hypothesis(
            current.id,
            statement=current.statement,
            expected_observations=current.expected_observations,
            refutation_criteria=current.refutation_criteria,
            status=update.status,
            update_reason=update.reason,
            actor="agent",
        )
        hypotheses[updated.id] = updated
        updated_hypotheses.append(_hypothesis_payload(updated))
        ctx.progress.record("hypothesis")

    message = _dump(
        {
            "status": "ok",
            "links": applied,
            "hypotheses": updated_hypotheses,
        }
    )
    await _finish_request(ctx, call_id, state=ToolRequestState.OBSERVED, message=message)
    return message


# --------------------------------------------------------------------------------------
# 动作请求
# --------------------------------------------------------------------------------------


async def _attempt_for(ctx: AgentContext, proposal_id: str) -> ActionAttempt | None:
    return await ctx.store.get_attempt(proposal_id)


async def _followup_ids(ctx: AgentContext, proposal_id: str) -> list[str]:
    items = await ctx.store.list_followups(proposal_id=proposal_id, limit=5)
    return [item.id for item in items]


def _action_feedback(
    ctx: AgentContext,
    proposal: ActionProposal,
    decision: RiskDecision | None,
    attempt: ActionAttempt | None,
    *,
    followup_ids: list[str],
    write_sent: bool,
) -> dict[str, Any]:
    verification = (attempt.verification if attempt else None) or {}
    disposition = decision.disposition.value if decision is not None else "recorded"
    reasons = decision.reasons if decision is not None else ["读取已保存的执行结果"]
    return {
        "status": "ok",
        "proposal_id": proposal.id,
        "attempt_id": attempt.id if attempt else None,
        "kind": proposal.kind.value,
        "purpose": proposal.purpose.value,
        "target": f"{proposal.target.namespace}/{proposal.target.kind}/{proposal.target.name}",
        "policy": {"disposition": disposition, "reasons": reasons},
        "write_sent": write_sent,
        "prediction": proposal.prediction,
        "observation_window_seconds": proposal.observation_window_seconds,
        "request_result": attempt.result if attempt else None,
        "business_before": verification.get("baseline"),
        "business_after": verification.get("post_tail") or verification.get("post"),
        "resource_change": {
            key: verification.get(key)
            for key in (
                "old_uid",
                "new_uid",
                "new_pod",
                "template_unchanged",
                "pod_ready",
                "pod_restarts",
                "memory_limit_restored",
                "ready_pods",
                "rolled_out",
            )
            if key in verification
        },
        "outcome": verification.get("outcome"),
        "outcome_reason": verification.get("outcome_reason"),
        "raw_refs": [item for item in [verification.get("raw_ref")] if item],
        "followup_ids": followup_ids,
        "note": (
            "业务恢复判定由执行器健康断言给出：只有动作后业务与资源检查都通过才算 improved。"
            "恢复只支持动作效果与限定假设，不自动证明更深层因果。"
        ),
    }


async def _execute_and_feedback(
    ctx: AgentContext,
    proposal: ActionProposal,
    decision: RiskDecision | None,
    *,
    actor: str,
) -> tuple[str, bool]:
    """在预算预留通过后执行已授权提案，并把结果压成反馈。"""
    executor = ctx.executor
    if executor is None:
        return _error("proposal_unavailable", "未配置动作执行器"), False
    bound = executor.adapter_upper_bound()
    try:
        ctx.budget.check_write_reserve(adapter_bound=bound, model_reserve=ctx.model_reserve_seconds)
    except WriteReserveUnavailable as exc:
        await executor.abandon(proposal.id, actor="budget-guard", reason=str(exc))
        return (
            _dump(
                {
                    "status": "not_started",
                    "code": "budget_reserve",
                    "message": str(exc),
                    "note": "没有发出写请求；提案已标记拒绝，可以继续只读调查或直接给出报告。",
                }
            ),
            False,
        )
    await ctx.budget.begin_action()
    try:
        attempt = await executor.execute_authorized(proposal.id, actor=actor)
    except ActionError as exc:
        attempt = await _attempt_for(ctx, proposal.id)
        if attempt is None:
            return (
                _dump(
                    {
                        "status": "error",
                        "code": exc.code,
                        "message": exc.message,
                        "write_sent": False,
                    }
                ),
                False,
            )
    finally:
        await ctx.budget.end_action()

    followups = await _followup_ids(ctx, proposal.id)
    feedback = _action_feedback(
        ctx, proposal, decision, attempt, followup_ids=followups, write_sent=True
    )
    message = _dump(feedback)
    if (
        attempt is not None
        and (attempt.verification or {}).get("outcome") == AttemptOutcome.IMPROVED.value
    ):
        ctx.progress.recovered = True
        await ctx.store.record_recovery(ctx.investigation_id, at=datetime.now(UTC))
    ctx.progress.record("action")
    return message, True


async def _validate_action_references(ctx: AgentContext, args: RequestActionArgs) -> str | None:
    """动作必须绑定同一调查内实际存在的非知识观测，并指向目标异常。"""
    hypothesis = await ctx.store.get_hypothesis(args.hypothesis_id)
    if hypothesis is None or hypothesis.investigation_id != ctx.investigation_id:
        return f"假设不存在或不属于本次调查：{args.hypothesis_id}"
    if hypothesis.status.value == "refuted":
        return "假设已被反驳，不能作为写动作依据；请先补充证据或改用其他假设"
    observations = {
        item.id: item for item in await ctx.store.list_observations(ctx.investigation_id)
    }
    links = await ctx.store.list_hypothesis_evidence(ctx.investigation_id)
    supporting = {
        link.observation_id
        for link in links
        if link.hypothesis_id == hypothesis.id and link.relation == "supporting"
    }
    calls = {item.id: item for item in await ctx.store.list_tool_calls(ctx.investigation_id)}
    has_platform = False
    has_business = False
    target_scoped = False
    target = args.target_pod or args.target_deployment
    for evidence_id in args.evidence_ids:
        item = observations.get(evidence_id)
        if item is None:
            return f"观测不存在或不属于本次调查：{evidence_id}"
        if item.source == "knowledge":
            return "runbook 不能作为写操作依据"
        if item.status is not ObservationStatus.OK:
            return f"观测 {evidence_id} 没有取到数据，不能作为写操作依据"
        if evidence_id not in supporting:
            return (
                f"观测 {evidence_id} 还没有被判定为支持假设 {hypothesis.id}；"
                "请先用 assess_evidence 记录 supporting 关系"
            )
        call = calls.get(item.tool_call_id)
        tool = call.tool if call else ""
        params = call.parameters if call else {}
        if tool.startswith("kubernetes."):
            has_platform = True
            selector = str(params.get("selector") or "")
            involved = str(params.get("involved_name") or "")
            if target and (target in selector or involved == target):
                target_scoped = True
        if tool == "prometheus.query_range":
            has_business = True
        if tool == "kubernetes.list_logs":
            has_business = True
            selector = str(params.get("selector") or "")
            involved = str(params.get("involved_name") or "")
            if target and (target in selector or involved == target):
                target_scoped = True
    if not has_platform:
        return "动作依据至少需要一条 Kubernetes 平台观测"
    if not has_business:
        return "动作依据至少需要一条业务侧观测（Prometheus 或目标 Pod 日志）"
    if args.action == "restart_pod" and not target_scoped:
        return (
            "缺少指向目标 Pod 的平台观测：重建只适用于该 Pod 自身的异常，"
            "依赖链路或入口现象不能作为重建依据"
        )
    return None


@tool
async def request_action(
    runtime: ToolRuntime[AgentContext],
    action: Literal["restart_pod", "rollout_undo"],
    reason: str,
    hypothesis_id: str,
    evidence_ids: list[str],
    purpose: Literal["diagnostic", "mitigation", "remediation"] = "remediation",
    target_pod: str = "",
    target_deployment: str = "",
    service: str = "",
    path: str = "/",
    prediction: str = "",
    absence_meaning: str = "",
    business_target: str = "",
    observation_window_seconds: int = 0,
) -> str:
    """提交一个动作请求：先落提案与风险判定，再按授权执行并返回结构化反馈。

    同一响应里只能有这一个动作请求，不能与其他工具混用。诊断目的必须说明预测、
    没有出现预测意味着什么以及业务验证目标；观察窗口由执行器配置决定。
    """
    try:
        args = RequestActionArgs(
            action=action,
            purpose=purpose,
            target_pod=target_pod,
            target_deployment=target_deployment,
            service=service,
            path=path,
            reason=reason,
            hypothesis_id=hypothesis_id,
            evidence_ids=evidence_ids,
            prediction=prediction,
            absence_meaning=absence_meaning,
            business_target=business_target,
            observation_window_seconds=observation_window_seconds,
        )
    except ValidationError as exc:
        return _error("invalid_argument", str(exc))

    ctx, call_id, cached = await _prepare_request(runtime, "request_action", args.model_dump())
    if cached is not None:
        return cached
    if ctx.progress.recovered:
        message = _error(
            "business_recovered",
            "业务已恢复，禁止新增动作；只能做只读收尾并给出报告",
        )
        await _finish_request(
            ctx,
            call_id,
            state=ToolRequestState.REJECTED,
            message=message,
            error_code="business_recovered",
        )
        return message

    existing = await ctx.store.get_tool_request(ctx.investigation_id, call_id)
    if existing is not None and existing.state is ToolRequestState.ACTION_PENDING:
        return await _resume_pending_action(ctx, call_id, existing)

    problem = await _validate_action_references(ctx, args)
    if problem is not None:
        message = _error("invalid_action_request", problem)
        await _finish_request(
            ctx,
            call_id,
            state=ToolRequestState.REJECTED,
            message=message,
            error_code="invalid_action_request",
        )
        return message

    executor = ctx.executor
    if executor is None:
        message = _error("proposal_unavailable", "未配置动作执行器")
        await _finish_request(
            ctx,
            call_id,
            state=ToolRequestState.REJECTED,
            message=message,
            error_code="proposal_unavailable",
        )
        return message

    purpose_enum = ActionPurpose(args.purpose)
    common = {
        "reason": args.reason,
        "actor": "model-agent",
        "namespace": ctx.namespace,
        "investigation_id": ctx.investigation_id,
        "origin": ActionOrigin.MODEL,
        "evidence_ids": args.evidence_ids,
        "purpose": purpose_enum,
        "hypothesis_id": args.hypothesis_id,
        "prediction": args.prediction,
        "absence_meaning": args.absence_meaning,
        "business_target": args.business_target,
        "observation_window_seconds": args.observation_window_seconds or None,
    }
    try:
        if args.action == "restart_pod":
            if not args.target_pod or not args.service:
                raise ActionError("invalid_argument", "restart_pod 需要目标 Pod 与验证 Service")
            proposal, decision, approval = await executor.prepare_restart_pod(
                pod=args.target_pod,
                service=args.service,
                path=args.path or "/",
                **common,
            )
        else:
            if not args.target_deployment:
                raise ActionError("invalid_argument", "rollout_undo 需要目标 Deployment")
            proposal, decision, approval = await executor.prepare_rollback(
                deployment=args.target_deployment,
                **common,
            )
    except AutoExecutionError as exc:
        # 旧路径才会自动执行；新循环只准备，这里仅记录异常以便追踪。
        logger.warning("准备动作时出现自动执行异常：{} {}", exc.code, exc.proposal_id)
        message = _error("prepare_error", exc.message)
        await _finish_request(
            ctx,
            call_id,
            state=ToolRequestState.REJECTED,
            message=message,
            error_code=exc.code,
        )
        return message
    except ActionError as exc:
        message = _dump(
            {
                "status": "inapplicable",
                "code": exc.code,
                "message": exc.message,
                "write_sent": False,
                "note": "动作不适用；可以继续只读调查或给出人工待办。",
            }
        )
        await _finish_request(
            ctx,
            call_id,
            state=ToolRequestState.REJECTED,
            message=message,
            error_code=exc.code,
        )
        ctx.progress.record("action_rejected")
        return message

    ctx.progress.action_state = {
        "proposal_id": proposal.id,
        "kind": proposal.kind.value,
        "disposition": decision.disposition.value,
    }
    if decision.disposition is RiskDisposition.DENY:
        feedback = _action_feedback(
            ctx, proposal, decision, None, followup_ids=[], write_sent=False
        )
        feedback["status"] = "denied"
        message = _dump(feedback)
        await _finish_request(
            ctx,
            call_id,
            state=ToolRequestState.REJECTED,
            message=message,
            error_code="policy_denied",
            proposal_id=proposal.id,
        )
        return message

    if decision.disposition is RiskDisposition.AUTO and approval is not None:
        message, _ = await _execute_and_feedback(ctx, proposal, decision, actor="model-agent")
        attempt = await _attempt_for(ctx, proposal.id)
        await _finish_request(
            ctx,
            call_id,
            state=ToolRequestState.ACTION_EXECUTED,
            message=message,
            proposal_id=proposal.id,
            action_attempt_id=attempt.id if attempt else None,
        )
        return message

    # 审批动作也要在暂停前确认恢复后仍有反馈模型调用；否则不要制造无法执行的等待任务。
    bound = executor.adapter_upper_bound()
    try:
        ctx.budget.check_write_reserve(adapter_bound=bound, model_reserve=ctx.model_reserve_seconds)
    except WriteReserveUnavailable as exc:
        await executor.abandon(proposal.id, actor="budget-guard", reason=str(exc))
        message = _dump(
            {
                "status": "not_started",
                "code": "budget_reserve",
                "message": str(exc),
                "write_sent": False,
                "note": "没有创建审批等待；可以继续只读调查或直接给出报告。",
            }
        )
        await _finish_request(
            ctx,
            call_id,
            state=ToolRequestState.REJECTED,
            message=message,
            error_code="budget_reserve",
            proposal_id=proposal.id,
        )
        return message

    # 需要人工审批：先落账本与任务暂停状态，再中断等待。
    expires = approval.expires_at if approval is not None else None
    await ctx.store.set_task_waiting(ctx.investigation_id, proposal_id=proposal.id)
    pause_payload = {
        "proposal_id": proposal.id,
        "kind": proposal.kind.value,
        "purpose": proposal.purpose.value,
        "target": f"{proposal.target.namespace}/{proposal.target.kind}/{proposal.target.name}",
        "prediction": proposal.prediction,
        "reasons": decision.reasons,
        "expires_at": expires.isoformat() if expires else None,
    }
    await _finish_request(
        ctx,
        call_id,
        state=ToolRequestState.ACTION_PENDING,
        message=_dump({"status": "waiting_approval", **pause_payload}),
        proposal_id=proposal.id,
    )
    await ctx.budget.pause()
    return _dump(interrupt(pause_payload))


async def _resume_pending_action(ctx: AgentContext, call_id: str, request: ToolRequest) -> str:
    """恢复进入工具：读账本与既有提案，不重新生成提案，也不重复执行。"""
    proposal_id = request.proposal_id or ""
    proposal = await ctx.store.get_proposal(proposal_id) if proposal_id else None
    if proposal is None:
        message = _error("proposal_missing", f"账本记录的提案不存在：{proposal_id}")
        await _finish_request(
            ctx,
            call_id,
            state=ToolRequestState.REJECTED,
            message=message,
            error_code="proposal_missing",
        )
        return message
    attempt = await _attempt_for(ctx, proposal.id)
    if attempt is not None:
        feedback = _action_feedback(
            ctx,
            proposal,
            None,
            attempt,
            followup_ids=await _followup_ids(ctx, proposal.id),
            write_sent=True,
        )
        feedback["status"] = "replayed"
        message = _dump(feedback)
        await _finish_request(
            ctx,
            call_id,
            state=ToolRequestState.ACTION_EXECUTED,
            message=message,
            proposal_id=proposal.id,
            action_attempt_id=attempt.id,
        )
        return message
    approval = await ctx.store.get_approval(proposal.id)
    if approval is None or proposal.status is ProposalStatus.PENDING_APPROVAL:
        # 还没有决定：再次停在同一个审批点，不执行。
        await ctx.budget.pause()
        return _dump(
            interrupt(
                {
                    "proposal_id": proposal.id,
                    "kind": proposal.kind.value,
                    "purpose": proposal.purpose.value,
                    "target": (
                        f"{proposal.target.namespace}/{proposal.target.kind}/{proposal.target.name}"
                    ),
                    "prediction": proposal.prediction,
                    "reasons": ["仍在等待人工审批"],
                    "expires_at": None,
                }
            )
        )
    if approval.decision.value == "rejected":
        message = _dump(
            {
                "status": "rejected",
                "proposal_id": proposal.id,
                "write_sent": False,
                "reason": approval.reason,
                "note": "人工拒绝；不能换一个提案 ID 自动重提同一意图，可继续只读调查。",
            }
        )
        await _finish_request(
            ctx,
            call_id,
            state=ToolRequestState.REJECTED,
            message=message,
            proposal_id=proposal.id,
            error_code="operator_rejected",
        )
        ctx.progress.record("action_rejected")
        return message
    message, _ = await _execute_and_feedback(ctx, proposal, None, actor=approval.actor)
    attempt = await _attempt_for(ctx, proposal.id)
    await _finish_request(
        ctx,
        call_id,
        state=ToolRequestState.ACTION_EXECUTED,
        message=message,
        proposal_id=proposal.id,
        action_attempt_id=attempt.id if attempt else None,
    )
    return message


__all__ = [
    "READ_TOOLS",
    "assess_evidence",
    "knowledge_get_runbook",
    "knowledge_list_runbooks",
    "kubernetes_list_deployments",
    "kubernetes_list_events",
    "kubernetes_list_logs",
    "kubernetes_list_pods",
    "kubernetes_list_services",
    "manage_hypotheses",
    "prometheus_query_range",
    "request_action",
]
