"""假设驱动调查循环：LangGraph 编排，checkpoint 存进度，业务记录进 PostgreSQL。"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TypedDict

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

from aiops.actions.executor import AutoExecutionError
from aiops.actions.rollout_undo import ActionError
from aiops.contracts import (
    ActionOrigin,
    ActionProposal,
    AuditEvent,
    Budget,
    Evidence,
    Hypothesis,
    Incident,
    Investigation,
    InvestigationStatus,
    RiskDecision,
    TimeRange,
    ToolCall,
    ToolResult,
)
from aiops.db import Store
from aiops.investigation.prompts import assess_messages, plan_messages
from aiops.investigation.schemas import Assessment, PlanRequest, QueryRequest
from aiops.investigation.toolbox import ToolboxProtocol, observation
from aiops.model import ModelCall, ModelError
from aiops.tools.base import ToolContext


class ActionProposer(Protocol):
    """把调查结论转成提案；策略判为自动允许时由执行器立即执行并验证。"""

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
    ) -> tuple[ActionProposal, RiskDecision]: ...

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
    ) -> tuple[ActionProposal, RiskDecision]: ...


class ModelProtocol(Protocol):
    async def complete_json[T: BaseModel](
        self,
        messages: list[dict[str, str]],
        response_model: type[T],
        *,
        schema_name: str = "response",
        temperature: float | None = None,
    ) -> ModelCall[T]: ...


class State(TypedDict, total=False):
    hypotheses: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    failures: list[dict[str, Any]]
    queries: list[dict[str, Any]]
    round: int
    finished: bool
    conclusion: str
    aborted: bool
    termination: str
    outcome: str
    proposal_id: str
    runbook_ids: list[str]
    runbook_uses: list[dict[str, Any]]


@dataclass(frozen=True)
class Report:
    investigation_id: str
    status: InvestigationStatus
    conclusion: str | None
    termination_reason: str | None
    hypotheses: list[dict[str, Any]]
    evidence_count: int
    tool_call_count: int
    proposal_id: str | None = None
    runbook_uses: list[dict[str, Any]] = field(default_factory=list)


class InvestigationRuntime:
    def __init__(
        self,
        *,
        store: Store,
        toolbox: ToolboxProtocol,
        model: ModelProtocol,
        dsn: str,
        incident: Incident,
        investigation: Investigation,
        observation_window: TimeRange,
        runtime_path: Path,
        proposer: ActionProposer | None = None,
        tool_timeout_seconds: float = 10.0,
        max_observation_chars: int = 4000,
    ) -> None:
        self._store = store
        self._toolbox = toolbox
        self._model = model
        self._proposer = proposer
        self._dsn = dsn
        self._incident = incident
        self._investigation = investigation
        self._investigation_id = investigation.id
        self._budget: Budget = investigation.budget
        self._window = observation_window
        self._runtime_path = runtime_path
        self._tool_timeout = tool_timeout_seconds
        self._max_observation_chars = max_observation_chars
        self._used_tool_calls = investigation.usage.tool_calls
        self._used_model_calls = investigation.usage.model_calls
        self._deadline = time.monotonic() + investigation.budget.max_wall_clock_seconds
        self._evidence_seq = 0

    async def run(self) -> Report:
        async with AsyncPostgresSaver.from_conn_string(self._dsn) as saver:
            await saver.setup()
            graph = self._build().compile(checkpointer=saver)
            final = await graph.ainvoke(
                {"hypotheses": [], "evidence": [], "failures": [], "queries": [], "round": 0},
                config={"configurable": {"thread_id": self._investigation_id}},
            )
        return await self._report(final)

    async def _report(self, final: State) -> Report:
        saved = await self._store.get_investigation(self._investigation_id)
        if saved is None:
            raise LookupError(f"调查不存在：{self._investigation_id}")
        return Report(
            investigation_id=self._investigation_id,
            status=saved.status,
            conclusion=saved.conclusion,
            termination_reason=saved.termination_reason,
            hypotheses=final.get("hypotheses") or [],
            evidence_count=len(final.get("evidence") or []),
            tool_call_count=self._used_tool_calls,
            proposal_id=final.get("proposal_id") or None,
            runbook_uses=final.get("runbook_uses") or [],
        )

    def _build(self):
        builder = StateGraph(State)
        builder.add_node("plan", self._plan)
        builder.add_node("collect", self._collect)
        builder.add_node("assess", self._assess)
        builder.add_node("conclude", self._conclude)
        builder.add_edge(START, "plan")
        builder.add_conditional_edges(
            "plan", self._after_prepare, {"collect": "collect", "conclude": "conclude"}
        )
        builder.add_conditional_edges(
            "collect", self._after_collect, {"assess": "assess", "conclude": "conclude"}
        )
        builder.add_conditional_edges(
            "assess", self._after_assess, {"collect": "collect", "conclude": "conclude"}
        )
        builder.add_edge("conclude", END)
        return builder

    def _after_prepare(self, state: State) -> str:
        return "conclude" if state.get("aborted") else "collect"

    def _after_collect(self, state: State) -> str:
        return "conclude" if state.get("aborted") else "assess"

    def _after_assess(self, state: State) -> str:
        if state.get("aborted") or state.get("finished"):
            return "conclude"
        return "collect" if state.get("queries") else "conclude"

    def _remaining(self) -> dict[str, int]:
        return {
            "tool_calls": max(0, self._budget.max_tool_calls - self._used_tool_calls),
            "model_calls": max(0, self._budget.max_model_calls - self._used_model_calls),
            "seconds": max(0, int(self._deadline - time.monotonic())),
        }

    def _model_budget_block(self) -> str | None:
        if self._used_model_calls >= self._budget.max_model_calls:
            return "model_calls"
        if time.monotonic() >= self._deadline:
            return "wall_clock"
        return None

    def _tool_budget_block(self) -> str | None:
        if self._used_tool_calls >= self._budget.max_tool_calls:
            return "tool_calls"
        if time.monotonic() >= self._deadline:
            return "wall_clock"
        return None

    def _incident_payload(self) -> dict[str, Any]:
        return self._incident.model_dump(mode="json")

    async def _plan(self, state: State) -> State:
        block = self._model_budget_block()
        if block is not None:
            return {"aborted": True, "termination": f"预算耗尽：{block}"}
        try:
            call = await self._model.complete_json(
                plan_messages(self._incident_payload(), self._remaining()),
                PlanRequest,
                schema_name="investigation_plan",
            )
        except ModelError as exc:
            return {
                "aborted": True,
                "termination": f"模型调用失败：{exc.code}（尝试 {exc.attempts} 次）",
            }
        self._used_model_calls += 1
        await self._store.add_usage(self._investigation_id, model_calls=1)

        hypotheses = []
        for draft in call.value.hypotheses:
            hypothesis = Hypothesis(
                id=f"hyp-{uuid.uuid4().hex[:12]}",
                investigation_id=self._investigation_id,
                statement=draft.statement,
                expected_observations=draft.expected_observations,
                origin="model",
            )
            await self._store.save_hypothesis(hypothesis)
            hypotheses.append(
                {
                    "id": hypothesis.id,
                    "statement": hypothesis.statement,
                    "expected_observations": hypothesis.expected_observations,
                    "status": hypothesis.status.value,
                }
            )
        return {
            "hypotheses": hypotheses,
            "queries": [query.model_dump() for query in call.value.queries],
            "round": 1,
        }

    async def _collect(self, state: State) -> State:
        hypotheses = list(state.get("hypotheses") or [])
        evidence = list(state.get("evidence") or [])
        failures = list(state.get("failures") or [])
        runbook_ids = list(state.get("runbook_ids") or [])

        for payload in state.get("queries") or []:
            block = self._tool_budget_block()
            if block is not None:
                return {
                    "evidence": evidence,
                    "failures": failures,
                    "queries": [],
                    "aborted": True,
                    "termination": f"预算耗尽：{block}",
                }
            request = QueryRequest(**payload)
            if request.hypothesis >= len(hypotheses):
                failures.append(
                    {
                        "tool": request.tool,
                        "purpose": request.purpose,
                        "error": "invalid_hypothesis",
                        "message": "假设下标超出当前列表",
                    }
                )
                continue

            context = self._tool_context()
            started_at = datetime.now(UTC)
            result = await self._toolbox.run(context, self._window, request)
            finished_at = datetime.now(UTC)
            await self._record_tool_call(request, result, started_at, finished_at)

            if result.error is not None:
                failures.append(
                    {
                        "tool": request.tool,
                        "purpose": request.purpose,
                        "error": result.error.code,
                        "message": result.error.message,
                    }
                )
                continue

            evidence.append(
                await self._record_evidence(request, hypotheses[request.hypothesis]["id"], result)
            )
            if request.tool.startswith("knowledge."):
                for runbook_id in self._runbook_ids(result):
                    if runbook_id not in runbook_ids:
                        runbook_ids.append(runbook_id)

        return {
            "evidence": evidence,
            "failures": failures,
            "queries": [],
            "runbook_ids": runbook_ids,
        }

    @staticmethod
    def _runbook_ids(result: ToolResult) -> list[str]:
        data = result.data or {}
        if "items" in data:
            return [str(item.get("id")) for item in data["items"] if item.get("id")]
        if data.get("id"):
            return [str(data["id"])]
        return []

    def _tool_context(self) -> ToolContext:
        remaining = self._deadline - time.monotonic()
        return ToolContext(
            investigation_id=self._investigation_id,
            cluster=self._incident.cluster,
            namespace=self._incident.namespace,
            runtime_path=self._runtime_path,
            deadline=time.monotonic() + min(self._tool_timeout, max(0.0, remaining)),
        )

    async def _record_tool_call(
        self, request: QueryRequest, result: ToolResult, started_at, finished_at
    ) -> None:
        self._used_tool_calls += 1
        await self._store.add_usage(self._investigation_id, tool_calls=1)
        await self._store.save_tool_call(
            ToolCall(
                id=result.tool_call_id,
                investigation_id=self._investigation_id,
                tool=request.tool,
                parameters=request.model_dump(),
                time_range=result.time_range,
                started_at=started_at,
                finished_at=finished_at,
                error_code=result.error.code if result.error else None,
                truncated=result.truncated,
                raw_ref=result.raw_ref,
            )
        )

    async def _record_evidence(
        self, request: QueryRequest, hypothesis_id: str, result: ToolResult
    ) -> dict[str, Any]:
        self._evidence_seq += 1
        evidence = Evidence(
            id=f"ev-{uuid.uuid4().hex[:12]}",
            investigation_id=self._investigation_id,
            hypothesis_id=hypothesis_id,
            tool_call_id=result.tool_call_id,
            source=result.source,
            independence_key=f"{result.source}:{request.tool}",
            observation=observation(result, max_chars=self._max_observation_chars),
            relation="neutral",
            raw_ref=result.raw_ref or "",
            time_range=result.time_range,
        )
        await self._store.save_evidence(evidence)
        return {
            "id": evidence.id,
            "hypothesis_id": hypothesis_id,
            "source": evidence.source,
            "observation": evidence.observation,
            "relation": evidence.relation,
        }

    async def _assess(self, state: State) -> State:
        block = self._model_budget_block()
        if block is not None:
            return {"aborted": True, "termination": f"预算耗尽：{block}"}
        hypotheses = list(state.get("hypotheses") or [])
        evidence = list(state.get("evidence") or [])
        failures = list(state.get("failures") or [])
        runbook_ids = list(state.get("runbook_ids") or [])
        messages = assess_messages(
            self._incident_payload(), hypotheses, evidence, failures, self._remaining()
        )
        assessment: Assessment | None = None
        for round_index in (1, 2):
            block = self._model_budget_block()
            if block is not None:
                return {"aborted": True, "termination": f"预算耗尽：{block}"}
            try:
                call = await self._model.complete_json(
                    messages,
                    Assessment,
                    schema_name="investigation_assessment",
                )
            except ModelError as exc:
                return {
                    "aborted": True,
                    "termination": f"模型调用失败：{exc.code}（尝试 {exc.attempts} 次）",
                }
            self._used_model_calls += 1
            await self._store.add_usage(self._investigation_id, model_calls=1)
            assessment = call.value

            cited = {use.runbook_id for use in assessment.runbook_uses}
            unknown_now = sorted(cited - set(runbook_ids))
            for runbook_id in unknown_now:
                await self._audit_runbook("runbook.unknown", runbook_id, "引用了未取回的条目")
            if not unknown_now:
                break
            if round_index == 2:
                return {
                    "aborted": True,
                    "termination": f"引用了未取回的 runbook：{','.join(unknown_now)}",
                }
            # 引用纪律失败不等于调查失败：给它一次改正机会，并把可用条目再讲一遍。
            available = "、".join(runbook_ids) if runbook_ids else "本次没有任何 runbook 候选"
            messages = [
                *messages,
                {"role": "assistant", "content": call.raw_text or "（上一条结构化输出已省略）"},
                {
                    "role": "user",
                    "content": (
                        f"上一条输出引用了未取回的 runbook：{','.join(unknown_now)}。"
                        f"可用条目：{available}。"
                        "只能引用可用条目；没有候选时 runbook_uses 必须为空列表，"
                        "不要写占位值。请重新输出完整的结构化结果。"
                    ),
                },
            ]
        for update in assessment.hypotheses:
            if update.index < len(hypotheses):
                hypotheses[update.index]["status"] = update.status
                await self._store.set_hypothesis_status(
                    hypotheses[update.index]["id"], update.status
                )
        for verdict in assessment.verdicts:
            if verdict.evidence_index < len(evidence):
                evidence[verdict.evidence_index]["relation"] = verdict.relation
                await self._store.set_evidence_relation(
                    evidence[verdict.evidence_index]["id"], verdict.relation
                )

        knowledge_only = assessment.root_cause and not any(
            item["relation"] == "supporting" and item["source"] != "knowledge" for item in evidence
        )

        runbook_uses = list(state.get("runbook_uses") or [])
        unknown = sorted(
            {use.runbook_id for use in assessment.runbook_uses if use.runbook_id not in runbook_ids}
        )
        for runbook_id in unknown:
            await self._audit_runbook("runbook.unknown", runbook_id, "引用了未取回的条目")
        if unknown:
            # 结论里可能已经写了这个条目，不能只丢掉结构化字段后继续。
            return {
                "aborted": True,
                "termination": f"引用了未取回的 runbook：{','.join(unknown)}",
            }
        for use in assessment.runbook_uses:
            event = "runbook.accepted" if use.accepted else "runbook.rejected"
            await self._audit_runbook(event, use.runbook_id, use.reason)
            runbook_uses.append(
                {
                    "runbook_id": use.runbook_id,
                    "accepted": use.accepted,
                    "reason": use.reason,
                }
            )
        if assessment.finish and runbook_ids:
            judged = {use.runbook_id for use in assessment.runbook_uses}
            missing = [runbook_id for runbook_id in runbook_ids if runbook_id not in judged]
            if missing:
                await self._audit_runbook("runbook.unjudged", ",".join(missing), "查询过但没有判定")
                return {
                    "aborted": True,
                    "termination": f"未判定已查询的 runbook：{','.join(missing)}",
                }
        # 提案放在所有结论合法性检查之后：自动允许会立即写集群，不能先执行再中止调查。
        proposal_id = ""
        if assessment.finish and assessment.action_kind != "none" and not knowledge_only:
            if not assessment.root_cause:
                # 没有根因候选时不允许提案；否则自相矛盾的输出也能进入审批。
                await self._note_action(
                    "no_root_cause", assessment.action_pod or assessment.action_deployment or None
                )
            elif not assessment.conclusion.strip():
                # 空结论会被最终状态判成 inconclusive，不能在这种状态之前执行动作。
                await self._note_action(
                    "empty_conclusion",
                    assessment.action_pod or assessment.action_deployment or None,
                )
            else:
                supporting = []
                for index in assessment.action_evidence:
                    if (
                        0 <= index < len(evidence)
                        and evidence[index]["relation"] == "supporting"
                        and evidence[index]["source"] != "knowledge"
                    ):
                        evidence_id = evidence[index]["id"]
                        if evidence_id not in supporting:
                            supporting.append(evidence_id)
                proposal_id = await self._propose_action(assessment, supporting) or ""

        if knowledge_only:
            # 只有 runbook 参考、没有集群证据时不能确认根因。
            await self._store.append_audit(
                [
                    AuditEvent(
                        id=f"audit-{uuid.uuid4().hex[:12]}",
                        correlation_id=self._investigation_id,
                        actor="investigation-runtime",
                        event="conclusion.rejected_knowledge_only",
                        occurred_at=datetime.now(UTC),
                        payload={"reason": "没有非 knowledge 来源的支撑证据"},
                    )
                ]
            )

        return {
            "hypotheses": hypotheses,
            "evidence": evidence,
            "queries": [query.model_dump() for query in assessment.next_queries],
            "finished": assessment.finish,
            "conclusion": (
                assessment.conclusion if assessment.root_cause and not knowledge_only else ""
            ),
            "proposal_id": proposal_id,
            "runbook_uses": runbook_uses,
            "runbook_ids": runbook_ids,
        }

    async def _audit_runbook(self, event: str, runbook_id: str, reason: str) -> None:
        await self._store.append_audit(
            [
                AuditEvent(
                    id=f"audit-{uuid.uuid4().hex[:12]}",
                    correlation_id=self._investigation_id,
                    actor="investigation-runtime",
                    event=event,
                    occurred_at=datetime.now(UTC),
                    payload={"runbook_id": runbook_id, "reason": reason[:300]},
                )
            ]
        )

    async def _propose_action(self, assessment: Assessment, supporting: list[str]) -> str | None:
        """模型只给出意图；规则判定后，实验命名空间的可丢弃 Pod 重建会立即执行。"""
        target = (
            assessment.action_pod
            if assessment.action_kind == "restart_pod"
            else assessment.action_deployment
        )
        missing = (
            assessment.action_kind == "restart_pod" and not assessment.action_service
        ) or not target
        if missing:
            await self._note_action("missing_target", target or None)
            return None
        if not supporting:
            await self._note_action("no_supporting_evidence", target)
            return None
        if self._proposer is None:
            await self._note_action("proposer_unavailable", target)
            return None
        reason = assessment.action_reason or assessment.conclusion
        try:
            if assessment.action_kind == "restart_pod":
                proposal, decision = await self._proposer.propose_restart_pod(
                    pod=assessment.action_pod,
                    service=assessment.action_service,
                    path=assessment.action_path or "/",
                    reason=reason,
                    actor="investigation-runtime",
                    namespace=self._incident.namespace,
                    investigation_id=self._investigation_id,
                    origin=ActionOrigin.MODEL,
                    evidence_ids=supporting,
                )
            else:
                proposal, decision = await self._proposer.propose_rollback(
                    deployment=assessment.action_deployment,
                    reason=reason,
                    actor="investigation-runtime",
                    namespace=self._incident.namespace,
                    investigation_id=self._investigation_id,
                    origin=ActionOrigin.MODEL,
                    evidence_ids=supporting,
                )
        except AutoExecutionError as exc:
            # 提案与授权已落库，只是执行没完成；保留 proposal_id，不能当成“未提案”。
            await self._store.append_audit(
                [
                    AuditEvent(
                        id=f"audit-{uuid.uuid4().hex[:12]}",
                        correlation_id=self._investigation_id,
                        actor="investigation-runtime",
                        event="action.proposed_but_not_executed",
                        occurred_at=datetime.now(UTC),
                        payload={"proposal_id": exc.proposal_id, "code": exc.code},
                    )
                ]
            )
            return exc.proposal_id
        except ActionError as exc:
            await self._note_action(exc.code, target)
            return None
        await self._store.append_audit(
            [
                AuditEvent(
                    id=f"audit-{uuid.uuid4().hex[:12]}",
                    correlation_id=self._investigation_id,
                    actor="investigation-runtime",
                    event="action.proposed_by_model",
                    occurred_at=datetime.now(UTC),
                    payload={
                        "proposal_id": proposal.id,
                        "kind": assessment.action_kind,
                        "target": target,
                        "disposition": decision.disposition.value,
                        "evidence_ids": supporting,
                    },
                )
            ]
        )
        return proposal.id

    async def _note_action(self, reason: str, deployment: str | None) -> None:
        await self._store.append_audit(
            [
                AuditEvent(
                    id=f"audit-{uuid.uuid4().hex[:12]}",
                    correlation_id=self._investigation_id,
                    actor="investigation-runtime",
                    event="action.skipped",
                    occurred_at=datetime.now(UTC),
                    payload={"reason": reason, "deployment": deployment},
                )
            ]
        )

    async def _conclude(self, state: State) -> State:
        finished_at = datetime.now(UTC)
        if state.get("aborted"):
            status = InvestigationStatus.ABORTED
            termination = state.get("termination") or "调查中止"
            conclusion = None
        elif state.get("finished") and state.get("conclusion"):
            status = InvestigationStatus.COMPLETED
            termination = None
            conclusion = state["conclusion"]
        else:
            status = InvestigationStatus.INCONCLUSIVE
            termination = state.get("termination") or "证据不足，未形成根因候选"
            conclusion = None

        await self._store.set_investigation_status(
            self._investigation_id,
            status,
            conclusion=conclusion,
            termination_reason=termination,
            finished_at=finished_at,
        )
        await self._store.append_audit(
            [
                AuditEvent(
                    id=f"audit-{uuid.uuid4().hex[:12]}",
                    correlation_id=self._investigation_id,
                    actor="investigation-runtime",
                    event=f"investigation.{status.value}",
                    occurred_at=finished_at,
                    payload={
                        "hypotheses": len(state.get("hypotheses") or []),
                        "evidence": len(state.get("evidence") or []),
                        "tool_calls": self._used_tool_calls,
                        "model_calls": self._used_model_calls,
                        "termination": termination,
                    },
                )
            ]
        )
        return {
            "outcome": status.value,
            "conclusion": conclusion or "",
            "termination": termination or "",
        }
