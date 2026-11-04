"""假设驱动调查循环：LangGraph 编排，checkpoint 存进度，业务记录进 PostgreSQL。"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TypedDict

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

from aiops.contracts import (
    AuditEvent,
    Budget,
    Evidence,
    Hypothesis,
    Incident,
    Investigation,
    InvestigationStatus,
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


@dataclass(frozen=True)
class Report:
    investigation_id: str
    status: InvestigationStatus
    conclusion: str | None
    termination_reason: str | None
    hypotheses: list[dict[str, Any]]
    evidence_count: int
    tool_call_count: int


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
        tool_timeout_seconds: float = 10.0,
        max_observation_chars: int = 4000,
    ) -> None:
        self._store = store
        self._toolbox = toolbox
        self._model = model
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
            return {"aborted": True, "termination": f"模型调用失败：{exc.code}"}
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

        return {"evidence": evidence, "failures": failures, "queries": []}

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
        try:
            call = await self._model.complete_json(
                assess_messages(
                    self._incident_payload(), hypotheses, evidence, failures, self._remaining()
                ),
                Assessment,
                schema_name="investigation_assessment",
            )
        except ModelError as exc:
            return {"aborted": True, "termination": f"模型调用失败：{exc.code}"}
        self._used_model_calls += 1
        await self._store.add_usage(self._investigation_id, model_calls=1)

        assessment = call.value
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

        return {
            "hypotheses": hypotheses,
            "evidence": evidence,
            "queries": [query.model_dump() for query in assessment.next_queries],
            "finished": assessment.finish,
            "conclusion": assessment.conclusion if assessment.root_cause else "",
        }

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
