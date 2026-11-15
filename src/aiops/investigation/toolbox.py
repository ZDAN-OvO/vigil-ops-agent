"""只读工具分发：拒绝越界参数，并把结果压成模型可读的观测文本。"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from aiops.contracts import TimeRange, ToolError, ToolResult
from aiops.tools.base import ToolContext
from aiops.tools.kubernetes import KubernetesRead
from aiops.tools.prometheus import PrometheusClient

MIN_LOOKBACK_SECONDS = 60
MAX_LOOKBACK_SECONDS = 3600
MIN_STEP_SECONDS = 5
MAX_STEP_SECONDS = 300
MAX_SELECTOR_LENGTH = 200
MAX_RUNBOOK_ITEMS = 3


class QueryRequest(BaseModel):
    """一次只读查询请求；由工具参数构造，不接受越界值。"""

    model_config = ConfigDict(extra="forbid")

    hypothesis: int = Field(default=0, ge=0, description="旧循环的假设下标，新循环不使用")
    tool: Literal[
        "prometheus.query_range",
        "kubernetes.list_deployments",
        "kubernetes.list_pods",
        "kubernetes.list_events",
        "kubernetes.list_services",
        "kubernetes.list_logs",
        "knowledge.list_runbooks",
        "knowledge.get_runbook",
    ]
    purpose: str = ""
    expression: str = ""
    lookback_seconds: int = 0
    step_seconds: int = 0
    selector: str = ""
    involved_name: str = ""


class ToolboxProtocol(Protocol):
    async def run(
        self, context: ToolContext, time_range: TimeRange, request: QueryRequest
    ) -> ToolResult: ...


def _invalid(request: QueryRequest, time_range: TimeRange, message: str) -> ToolResult:
    return ToolResult(
        tool_call_id=uuid.uuid4().hex,
        source=request.tool,
        query=request.model_dump(),
        time_range=time_range,
        collected_at=datetime.now(UTC),
        error=ToolError(code="invalid_argument", message=message, retryable=False),
    )


def observation(result: ToolResult, *, max_chars: int = 4000) -> str:
    if result.error is not None:
        return f"查询失败（{result.error.code}）：{result.error.message}"
    text = json.dumps(result.data, ensure_ascii=False, default=str)
    if len(text) > max_chars:
        text = text[:max_chars] + "…（已截断）"
    if result.raw_ref:
        return f"{text}\n原始引用：{result.raw_ref}"
    return text


class RunbookReader(Protocol):
    async def list_runbooks(
        self, *, service: str | None = None, query: str | None = None, limit: int = 3
    ) -> list: ...

    async def get_runbook(self, runbook_id: str) -> object | None: ...


class Toolbox:
    def __init__(
        self,
        prometheus: PrometheusClient,
        kubernetes: KubernetesRead,
        runbooks: RunbookReader | None = None,
    ) -> None:
        self._prometheus = prometheus
        self._kubernetes = kubernetes
        self._runbooks = runbooks

    async def run(
        self, context: ToolContext, time_range: TimeRange, request: QueryRequest
    ) -> ToolResult:
        match request.tool:
            case "prometheus.query_range":
                return await self._prometheus_query(context, time_range, request)
            case "kubernetes.list_deployments":
                return await self._kubernetes.list_deployments(
                    context, time_range, selector=request.selector or None
                )
            case "kubernetes.list_pods":
                return await self._kubernetes.list_pods(
                    context, time_range, selector=request.selector or None
                )
            case "kubernetes.list_events":
                return await self._kubernetes.list_events(
                    context, time_range, involved_name=request.involved_name or None
                )
            case "kubernetes.list_services":
                return await self._kubernetes.list_services(
                    context, time_range, selector=request.selector or None
                )
            case "kubernetes.list_logs":
                return await self._kubernetes.list_logs(
                    context,
                    time_range,
                    selector=request.selector or None,
                    pod_name=request.involved_name or None,
                )
            case "knowledge.list_runbooks":
                return await self._list_runbooks(time_range, request)
            case "knowledge.get_runbook":
                return await self._get_runbook(time_range, request)
        return _invalid(request, time_range, f"未注册的工具：{request.tool}")

    def _knowledge_result(
        self, request: QueryRequest, time_range: TimeRange, data: dict
    ) -> ToolResult:
        return ToolResult(
            tool_call_id=uuid.uuid4().hex,
            source="knowledge",
            query=request.model_dump(),
            time_range=time_range,
            collected_at=datetime.now(UTC),
            data=data,
        )

    async def _list_runbooks(self, time_range: TimeRange, request: QueryRequest) -> ToolResult:
        if self._runbooks is None:
            return _invalid(request, time_range, "runbook 未登记")
        items = await self._runbooks.list_runbooks(
            service=request.selector or None,
            query=request.expression.strip() or None,
            limit=MAX_RUNBOOK_ITEMS,
        )
        return self._knowledge_result(
            request, time_range, {"items": [item.as_list_item() for item in items]}
        )

    async def _get_runbook(self, time_range: TimeRange, request: QueryRequest) -> ToolResult:
        if self._runbooks is None:
            return _invalid(request, time_range, "runbook 未登记")
        runbook_id = request.involved_name.strip()
        if not runbook_id:
            return _invalid(request, time_range, "involved_name 需要填 runbook id")
        runbook = await self._runbooks.get_runbook(runbook_id)
        if runbook is None:
            return _invalid(request, time_range, f"runbook 不存在：{runbook_id}")
        return self._knowledge_result(request, time_range, runbook.as_detail())

    async def _prometheus_query(
        self, context: ToolContext, time_range: TimeRange, request: QueryRequest
    ) -> ToolResult:
        if not request.expression.strip():
            return _invalid(request, time_range, "expression 不能为空")
        if not MIN_LOOKBACK_SECONDS <= request.lookback_seconds <= MAX_LOOKBACK_SECONDS:
            return _invalid(
                request,
                time_range,
                f"lookback_seconds 必须在 {MIN_LOOKBACK_SECONDS} 到 {MAX_LOOKBACK_SECONDS} 之间",
            )
        if not MIN_STEP_SECONDS <= request.step_seconds <= MAX_STEP_SECONDS:
            return _invalid(
                request,
                time_range,
                f"step_seconds 必须在 {MIN_STEP_SECONDS} 到 {MAX_STEP_SECONDS} 之间",
            )
        end = datetime.now(UTC)
        window = TimeRange(
            start=end - timedelta(seconds=request.lookback_seconds),
            end=end,
        )
        return await self._prometheus.query_range(
            context, request.expression, window, step_seconds=request.step_seconds
        )
