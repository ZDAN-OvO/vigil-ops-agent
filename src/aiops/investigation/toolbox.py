"""只读工具分发：拒绝越界参数，并把结果压成模型可读的观测文本。"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Protocol

from aiops.contracts import TimeRange, ToolError, ToolResult
from aiops.investigation.schemas import QueryRequest
from aiops.tools.base import ToolContext
from aiops.tools.kubernetes import KubernetesRead
from aiops.tools.prometheus import PrometheusClient

MIN_LOOKBACK_SECONDS = 60
MAX_LOOKBACK_SECONDS = 3600
MIN_STEP_SECONDS = 5
MAX_STEP_SECONDS = 300
MAX_SELECTOR_LENGTH = 200


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


class Toolbox:
    def __init__(self, prometheus: PrometheusClient, kubernetes: KubernetesRead) -> None:
        self._prometheus = prometheus
        self._kubernetes = kubernetes

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
        return _invalid(request, time_range, f"未注册的工具：{request.tool}")

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
