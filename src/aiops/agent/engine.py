"""单 Agent 调查运行：LangChain create_agent + 官方 PostgreSQL checkpoint。

同一个逻辑 Agent 完成取证、假设维护、证据判定、动作请求和执行后复核；业务记录
（调查、观测、关系、提案、执行、账本）进业务表，Agent 进度进 checkpoint 表。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from loguru import logger

from aiops.actions.executor import ActionExecutor
from aiops.agent.budget import BudgetExhausted, BudgetLedger
from aiops.agent.context import AgentContext, Progress
from aiops.agent.middleware import (
    ActionBatchMiddleware,
    BudgetMiddleware,
    InvalidReport,
    ModelFailure,
    NoProgress,
    ProgressMiddleware,
    ReportErrorGuard,
    RunCancelled,
)
from aiops.agent.prompts import (
    SYSTEM_PROMPT,
    correction_message,
    initial_message,
    missing_report_message,
)
from aiops.agent.schemas import ReportDraft
from aiops.agent.tools import (
    assess_evidence,
    knowledge_get_runbook,
    knowledge_list_runbooks,
    kubernetes_list_deployments,
    kubernetes_list_events,
    kubernetes_list_logs,
    kubernetes_list_pods,
    kubernetes_list_services,
    manage_hypotheses,
    prometheus_query_range,
    request_action,
)
from aiops.config import Settings
from aiops.contracts import (
    AGENT_RUNTIME_VERSION,
    AttemptStatus,
    AuditEvent,
    Incident,
    Investigation,
    InvestigationStatus,
    InvestigationTask,
    ObservationStatus,
    ProposalStatus,
    TaskOutcome,
    TaskPhase,
    TerminationCode,
)
from aiops.db import Store
from aiops.investigation.toolbox import Toolbox
from aiops.report import Report

AGENT_TOOLS = [
    prometheus_query_range,
    kubernetes_list_deployments,
    kubernetes_list_pods,
    kubernetes_list_events,
    kubernetes_list_services,
    kubernetes_list_logs,
    knowledge_list_runbooks,
    knowledge_get_runbook,
    manage_hypotheses,
    assess_evidence,
    request_action,
]

ACTION_NEEDS_HUMAN = {
    "no_improvement": TerminationCode.ACTION_NO_IMPROVEMENT,
    "worsened": TerminationCode.ACTION_WORSENED,
    "unknown": TerminationCode.ACTION_UNKNOWN,
}


class LeaseConflict(RuntimeError):
    """同一任务已有驱动者；不得并发驱动同一 Agent。"""


class WriteIntentUnknown(RuntimeError):
    """存在未完成的写意图；不能接管后重放动作。"""


class TaskNotResumable(RuntimeError):
    pass


class LangChainInvestigation:
    def __init__(
        self,
        *,
        settings: Settings,
        store: Store,
        dsn: str,
        incident: Incident,
        investigation: Investigation,
        toolbox: Toolbox,
        executor: ActionExecutor | None,
        runtime_path: Path | None = None,
        model: ChatOpenAI | None = None,
    ) -> None:
        if settings.model_name is None:
            raise ValueError("未配置 VIGIL_MODEL_NAME")
        self._settings = settings
        self._store = store
        self._dsn = dsn
        self._incident = incident
        self._investigation = investigation
        self._investigation_id = investigation.id
        self._toolbox = toolbox
        self._executor = executor
        self._runtime_path = runtime_path or settings.runtime_path
        self._model = model or self._build_model(settings)
        self._guard = ReportErrorGuard()
        self._lease_owner = f"agent-{uuid.uuid4().hex[:10]}"

    def _build_model(self, settings: Settings) -> ChatOpenAI:
        api_key = (
            settings.model_api_key.get_secret_value().strip()
            if settings.model_api_key is not None
            else ""
        )
        return ChatOpenAI(
            model=settings.model_name or "",
            base_url=settings.model_base_url,
            # 本地网关不校验密钥；OpenAI SDK 仍要求非空字符串。
            api_key=api_key or "sk-vigil-local-no-auth",
            timeout=settings.model_timeout_seconds,
            # 关闭 provider 隐藏重试，重试统一由预算中间件管理。
            max_retries=0,
            temperature=0,
        )

    # ------------------------------------------------------------------ 装配

    async def _ensure_task(self) -> InvestigationTask:
        existing = await self._store.get_task(self._investigation_id)
        if existing is not None:
            return existing
        await self._store.create_task(
            InvestigationTask(
                investigation_id=self._investigation_id,
                phase=TaskPhase.RUNNING,
                runtime_version=AGENT_RUNTIME_VERSION,
                calendar_started_at=self._investigation.started_at,
            )
        )
        created = await self._store.get_task(self._investigation_id)
        if created is None:
            raise LookupError(f"任务记录创建失败：{self._investigation_id}")
        return created

    async def _open_context(self) -> tuple[InvestigationTask, AgentContext]:
        task = await self._ensure_task()
        ledger = BudgetLedger.restore(
            store=self._store,
            investigation_id=self._investigation_id,
            budget=self._investigation.budget,
            task=task,
        )
        context = AgentContext(
            store=self._store,
            incident=self._incident,
            investigation_id=self._investigation_id,
            window=self._incident.time_range,
            budget=ledger,
            toolbox=self._toolbox,
            executor=self._executor,
            runtime_path=self._runtime_path,
            max_observation_chars=self._settings.investigation_max_observation_chars,
            tool_timeout_seconds=self._settings.tool_timeout_seconds,
            model_reserve_seconds=float(self._settings.investigation_action_model_reserve_seconds),
            progress=Progress(),
        )
        return task, context

    def _build_graph(self, saver: AsyncPostgresSaver):
        return create_agent(
            self._model,
            tools=AGENT_TOOLS,
            system_prompt=SYSTEM_PROMPT,
            response_format=ToolStrategy(ReportDraft, handle_errors=self._guard.handle),
            middleware=[
                ActionBatchMiddleware(),
                BudgetMiddleware(
                    configured_timeout=self._settings.model_timeout_seconds,
                    max_retries=self._settings.model_max_retries,
                ),
                ProgressMiddleware(),
            ],
            context_schema=AgentContext,
            checkpointer=saver,
        ).with_config({"recursion_limit": 200})

    def _config(self) -> dict[str, Any]:
        return {"configurable": {"thread_id": self._investigation_id}, "recursion_limit": 200}

    async def _acquire_lease(self, task: InvestigationTask, *, resuming: bool) -> None:
        if not await self._store.acquire_task_lease(
            self._investigation_id, self._lease_owner, seconds=300
        ):
            raise LeaseConflict("同一调查已有运行中的驱动者")
        if resuming and task.phase is not TaskPhase.WAITING_APPROVAL:
            if await self._store.task_has_write_intent(self._investigation_id):
                await self._store.release_task_lease(self._investigation_id, self._lease_owner)
                raise WriteIntentUnknown("存在未完成的写意图，结果可能未知；不接管、不重放写请求")

    # ------------------------------------------------------------------ 运行

    async def run(self) -> Report:
        task, context = await self._open_context()
        if task.phase is TaskPhase.TERMINAL:
            return await self._report(context, model_summary_completed=True)
        if task.cancel_requested_at is not None:
            return await self._abort(context, TerminationCode.CANCELLED, "任务在启动前已被取消")
        await self._acquire_lease(task, resuming=False)
        try:
            await context.budget.start()
            async with AsyncPostgresSaver.from_conn_string(self._dsn) as saver:
                await saver.setup()
                graph = self._build_graph(saver)
                result = await self._invoke(
                    graph,
                    {
                        "messages": [
                            HumanMessage(
                                initial_message(
                                    self._incident.model_dump(mode="json"),
                                    context.budget.remaining(),
                                )
                            )
                        ]
                    },
                    context,
                )
                if isinstance(result, Report):
                    return result
                return await self._finish_graph(graph, result, context)
        finally:
            await self._store.release_task_lease(self._investigation_id, self._lease_owner)

    async def resume(self, *, proposal_id: str, decision: str, actor: str = "operator") -> Report:
        task = await self._store.get_task(self._investigation_id)
        if task is None:
            raise TaskNotResumable("该调查不是单 Agent 任务，没有可恢复的 checkpoint")
        if task.phase is TaskPhase.TERMINAL:
            raise TaskNotResumable(f"任务已结束：{task.outcome}")
        await self._acquire_lease(task, resuming=True)
        try:
            _, context = await self._open_context()
            if task.cancel_requested_at is not None:
                return await self._abort(
                    context, TerminationCode.CANCELLED, "任务已被取消，不恢复执行"
                )
            if task.phase is TaskPhase.WAITING_APPROVAL:
                await self._store.resume_task(self._investigation_id)
            await context.budget.start()
            async with AsyncPostgresSaver.from_conn_string(self._dsn) as saver:
                await saver.setup()
                graph = self._build_graph(saver)
                state = await graph.aget_state(self._config())
                if not state.interrupts:
                    raise TaskNotResumable("没有待恢复的中断点；任务可能在运行中崩溃，需人工核实")
                result = await self._invoke(
                    graph,
                    Command(
                        resume={
                            "proposal_id": proposal_id,
                            "decision": decision,
                            "actor": actor,
                        }
                    ),
                    context,
                )
                if isinstance(result, Report):
                    return result
                return await self._finish_graph(graph, result, context)
        finally:
            await self._store.release_task_lease(self._investigation_id, self._lease_owner)

    async def _invoke(self, graph, payload, context: AgentContext) -> dict[str, Any] | Report:
        """执行一次图调用；把运行故障改写成确定性终态，不伪装成调查结论。"""
        try:
            return await graph.ainvoke(payload, self._config(), context=context)
        except RunCancelled:
            return await self._abort(context, TerminationCode.CANCELLED, "任务收到取消请求")
        except BudgetExhausted as exc:
            if context.progress.recovered:
                return await self._complete(
                    context,
                    draft=None,
                    outcome=TaskOutcome.RECOVERED,
                    code=TerminationCode.BUSINESS_RECOVERED,
                    conclusion=None,
                    model_summary_completed=False,
                    detail=f"业务已恢复，但预算耗尽后未完成模型总结：{exc.item}",
                )
            return await self._abort(
                context, TerminationCode.BUDGET_EXHAUSTED, f"预算耗尽：{exc.item}"
            )
        except NoProgress:
            if context.progress.recovered:
                return await self._complete(
                    context,
                    draft=None,
                    outcome=TaskOutcome.RECOVERED,
                    code=TerminationCode.BUSINESS_RECOVERED,
                    conclusion=None,
                    model_summary_completed=False,
                    detail="模型收尾未完成：连续三个轮次没有新进展",
                )
            return await self._complete(
                context,
                draft=None,
                outcome=TaskOutcome.INCONCLUSIVE,
                code=TerminationCode.NO_PROGRESS,
                conclusion=None,
                model_summary_completed=False,
                detail="连续三个模型轮次没有新观测、有效假设变更或动作状态变化",
            )
        except ModelFailure as exc:
            if context.progress.recovered:
                return await self._complete(
                    context,
                    draft=None,
                    outcome=TaskOutcome.RECOVERED,
                    code=TerminationCode.BUSINESS_RECOVERED,
                    conclusion=None,
                    model_summary_completed=False,
                    detail=f"模型收尾失败：{exc}",
                )
            return await self._abort(context, TerminationCode.MODEL_FAILURE, str(exc))
        except InvalidReport as exc:
            if context.progress.recovered:
                return await self._complete(
                    context,
                    draft=None,
                    outcome=TaskOutcome.RECOVERED,
                    code=TerminationCode.BUSINESS_RECOVERED,
                    conclusion=None,
                    model_summary_completed=False,
                    detail=str(exc),
                )
            return await self._complete(
                context,
                draft=None,
                outcome=TaskOutcome.INCONCLUSIVE,
                code=TerminationCode.INVALID_REPORT,
                conclusion=None,
                model_summary_completed=False,
                detail=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - 运行故障必须落成可追踪终态
            logger.exception("Agent 运行异常：{}", exc.__class__.__name__)
            return await self._abort(
                context,
                TerminationCode.RUNTIME_ERROR,
                f"运行时异常：{exc.__class__.__name__}",
            )

    async def _finish_graph(self, graph, result: dict[str, Any], context: AgentContext) -> Report:
        if result.get("__interrupt__"):
            await context.budget.pause()
            return await self._report(context, model_summary_completed=True)

        draft: ReportDraft | None = result.get("structured_response")
        problems = await self._validate_draft(draft, context) if draft else []
        if draft is not None and problems:
            try:
                context.budget.check_model_budget()
            except BudgetExhausted as exc:
                return await self._abort(
                    context, TerminationCode.BUDGET_EXHAUSTED, f"预算耗尽：{exc.item}"
                )
            result = await self._invoke(
                graph,
                {"messages": [HumanMessage(correction_message(problems))]},
                context,
            )
            if isinstance(result, Report):
                return result
            if result.get("__interrupt__"):
                await context.budget.pause()
                return await self._report(context, model_summary_completed=True)
            draft = result.get("structured_response")
            problems = await self._validate_draft(draft, context) if draft else ["缺少结构化报告"]

        if draft is None:
            try:
                context.budget.check_model_budget()
            except BudgetExhausted:
                pass
            else:
                result = await self._invoke(
                    graph, {"messages": [HumanMessage(missing_report_message())]}, context
                )
                if isinstance(result, Report):
                    return result
                if result.get("__interrupt__"):
                    await context.budget.pause()
                    return await self._report(context, model_summary_completed=True)
                draft = result.get("structured_response")
                problems = await self._validate_draft(draft, context) if draft else []

        if draft is None or problems:
            detail = "；".join(problems) if problems else "模型没有提交结构化报告"
            if context.progress.recovered:
                return await self._complete(
                    context,
                    draft=None,
                    outcome=TaskOutcome.RECOVERED,
                    code=TerminationCode.BUSINESS_RECOVERED,
                    conclusion=None,
                    model_summary_completed=False,
                    detail=detail,
                )
            return await self._complete(
                context,
                draft=None,
                outcome=TaskOutcome.INCONCLUSIVE,
                code=TerminationCode.INVALID_REPORT,
                conclusion=None,
                model_summary_completed=False,
                detail=detail,
            )
        return await self._decide_outcome(context, draft)

    async def _validate_draft(self, draft: ReportDraft, context: AgentContext) -> list[str]:
        problems: list[str] = []
        observations = {
            item.id: item for item in await self._store.list_observations(self._investigation_id)
        }
        if draft.root_cause_confirmed:
            if not draft.conclusion.strip():
                problems.append("root_cause_confirmed=true 但 conclusion 为空")
            if not draft.supporting_observation_ids:
                problems.append("根因候选必须引用至少一条支持观测")
            if not draft.evidence_boundary.strip():
                problems.append("根因候选必须写明证据边界")
        for observation_id in [
            *draft.supporting_observation_ids,
            *draft.contradictory_observation_ids,
        ]:
            item = observations.get(observation_id)
            if item is None:
                problems.append(f"引用了不存在的观测：{observation_id}")
            elif item.source == "knowledge":
                problems.append(f"runbook 不能作为根因证据：{observation_id}")
            elif item.status is not ObservationStatus.OK:
                problems.append(f"观测 {observation_id} 没有取到数据，不能作为证据")
        return problems

    async def _decide_outcome(self, context: AgentContext, draft: ReportDraft) -> Report:
        context.progress.needs_human = list(draft.needs_human)
        task = await self._store.get_task(self._investigation_id)
        recovered = context.progress.recovered or (
            task is not None and task.recovered_at is not None
        )
        action_outcome = await self._last_action_outcome()
        conclusion = draft.conclusion if draft.root_cause_confirmed else None
        if recovered:
            return await self._complete(
                context,
                draft=draft,
                outcome=TaskOutcome.RECOVERED,
                code=TerminationCode.BUSINESS_RECOVERED,
                conclusion=conclusion,
            )
        if action_outcome in ACTION_NEEDS_HUMAN:
            return await self._complete(
                context,
                draft=draft,
                outcome=TaskOutcome.NEEDS_HUMAN,
                code=ACTION_NEEDS_HUMAN[action_outcome],
                conclusion=conclusion,
            )
        if draft.root_cause_confirmed:
            return await self._complete(
                context,
                draft=draft,
                outcome=TaskOutcome.DIAGNOSIS_ONLY,
                code=TerminationCode.NO_APPLICABLE_ACTION,
                conclusion=draft.conclusion,
            )
        return await self._complete(
            context,
            draft=draft,
            outcome=TaskOutcome.INCONCLUSIVE,
            code=TerminationCode.INSUFFICIENT_EVIDENCE,
            conclusion=None,
        )

    async def _last_action_outcome(self) -> str | None:
        proposals = await self._store.list_proposals_for_investigation(self._investigation_id)
        for proposal in reversed(proposals):
            attempt = await self._store.get_attempt(proposal.id)
            if attempt is None:
                continue
            outcome = (attempt.verification or {}).get("outcome")
            return str(outcome) if outcome else None
        return None

    async def _complete(
        self,
        context: AgentContext,
        *,
        draft: ReportDraft | None,
        outcome: TaskOutcome,
        code: TerminationCode,
        conclusion: str | None,
        model_summary_completed: bool = True,
        detail: str = "",
    ) -> Report:
        await context.budget.pause()
        finished_at = datetime.now(UTC)
        if conclusion:
            status = InvestigationStatus.COMPLETED
            termination_reason = None
        elif outcome is TaskOutcome.RECOVERED:
            status = InvestigationStatus.INCONCLUSIVE
            termination_reason = "业务已恢复，但未形成可引用的根因候选"
        else:
            status = InvestigationStatus.INCONCLUSIVE
            termination_reason = detail or "证据不足，未形成根因候选"
        await self._store.set_investigation_status(
            self._investigation_id,
            status,
            conclusion=conclusion,
            termination_reason=termination_reason,
            finished_at=finished_at,
        )
        await self._store.finish_task(
            self._investigation_id,
            outcome=outcome,
            termination_code=code.value,
            finished_at=finished_at,
        )
        await self._audit(status, outcome, code, detail)
        return await self._report(context, model_summary_completed=model_summary_completed)

    async def _abort(
        self, context: AgentContext | None, code: TerminationCode, detail: str
    ) -> Report:
        finished_at = datetime.now(UTC)
        await self._store.set_investigation_status(
            self._investigation_id,
            InvestigationStatus.ABORTED,
            conclusion=None,
            termination_reason=detail,
            finished_at=finished_at,
        )
        await self._store.finish_task(
            self._investigation_id,
            outcome=TaskOutcome.ABORTED,
            termination_code=code.value,
            finished_at=finished_at,
        )
        await self._audit(InvestigationStatus.ABORTED, TaskOutcome.ABORTED, code, detail)
        if context is None:
            _, context = await self._open_context()
        return await self._report(context, model_summary_completed=False)

    async def _audit(self, status, outcome, code, detail: str) -> None:
        await self._store.append_audit(
            [
                AuditEvent(
                    id=f"audit-{uuid.uuid4().hex[:12]}",
                    correlation_id=self._investigation_id,
                    actor="agent-runtime",
                    event=f"investigation.{status.value}",
                    occurred_at=datetime.now(UTC),
                    payload={
                        "outcome": outcome.value,
                        "termination_code": code.value,
                        "detail": detail[:500],
                        "runtime_version": AGENT_RUNTIME_VERSION,
                    },
                )
            ]
        )

    # ------------------------------------------------------------------ 报告

    async def _report(self, context: AgentContext, *, model_summary_completed: bool) -> Report:
        saved = await self._store.get_investigation(self._investigation_id)
        if saved is None:
            raise LookupError(f"调查不存在：{self._investigation_id}")
        task = await self._store.get_task(self._investigation_id)
        hypotheses = await self._store.list_hypotheses(self._investigation_id)
        observations = await self._store.list_observations(self._investigation_id)
        relations = await self._store.list_hypothesis_evidence(self._investigation_id)
        tools = await self._store.list_tool_calls(self._investigation_id)
        proposals = await self._store.list_proposals_for_investigation(self._investigation_id)
        latest = proposals[-1] if proposals else None
        prediction = None
        if latest is not None and latest.prediction:
            attempt = await self._store.get_attempt(latest.id)
            verification = (attempt.verification if attempt else None) or {}
            prediction = {
                "proposal_id": latest.id,
                "purpose": latest.purpose.value,
                "prediction": latest.prediction,
                "observation_conditions": latest.observation_conditions,
                "observation_window_seconds": latest.observation_window_seconds,
                "outcome": verification.get("outcome"),
                "outcome_reason": verification.get("outcome_reason"),
            }
        needs_human = list(context.progress.needs_human)
        for proposal in proposals:
            attempt = await self._store.get_attempt(proposal.id)
            if attempt is None:
                continue
            if attempt.status in {AttemptStatus.FAILED, AttemptStatus.UNKNOWN} and (
                proposal.status in {ProposalStatus.FAILED, ProposalStatus.UNKNOWN}
            ):
                needs_human.append(
                    f"{proposal.id}: 执行结果 {(attempt.verification or {}).get('outcome')}"
                )
        task_payload = None
        if task is not None:
            task_payload = {
                "phase": task.phase.value,
                "outcome": task.outcome.value if task.outcome else None,
                "termination_code": task.termination_code,
                "runtime_version": task.runtime_version,
                "proposal_id": task.proposal_id,
                "no_progress_rounds": task.no_progress_rounds,
                "recovered_at": task.recovered_at.isoformat() if task.recovered_at else None,
                "investigation_seconds": round(task.investigation_seconds, 1),
                "action_seconds": round(task.action_seconds, 1),
                "calendar_seconds": round(context.budget.calendar_seconds(), 1),
                "used_model_calls": task.used_model_calls,
                "used_tool_calls": task.used_tool_calls,
            }
        return Report(
            investigation_id=self._investigation_id,
            status=saved.status,
            conclusion=saved.conclusion,
            termination_reason=saved.termination_reason,
            hypotheses=[
                {
                    "id": item.id,
                    "parent_id": item.parent_id,
                    "statement": item.statement,
                    "status": item.status.value,
                    "revision": item.revision,
                    "expected_observations": item.expected_observations,
                    "refutation_criteria": item.refutation_criteria,
                }
                for item in hypotheses
            ],
            evidence_count=len(observations),
            tool_call_count=len(tools),
            proposal_id=latest.id if latest else None,
            task=task_payload,
            action_ids=[item.id for item in proposals],
            relations=[
                {
                    "hypothesis_id": item.hypothesis_id,
                    "observation_id": item.observation_id,
                    "relation": item.relation,
                    "rationale": item.rationale,
                }
                for item in relations
            ],
            observations=[
                {
                    "id": item.id,
                    "source": item.source,
                    "status": item.status.value,
                    "summary": item.summary[:300],
                    "raw_ref": item.raw_ref,
                }
                for item in observations
            ],
            remaining_budget=context.budget.remaining(),
            needs_human=needs_human,
            prediction=prediction,
            model_summary_completed=model_summary_completed,
        )


__all__ = ["LangChainInvestigation", "LeaseConflict", "Report", "TaskNotResumable"]
