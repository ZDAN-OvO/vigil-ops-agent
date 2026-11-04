"""Prometheus 只读查询：限制点数、保留原始响应、区分空结果与失败。"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any, Self

import httpx

from aiops.contracts import TimeRange, ToolError, ToolResult
from aiops.tools.base import ToolContext


def _number(raw: str) -> float | str:
    if raw in {"NaN", "+Inf", "-Inf"}:
        return raw
    return float(raw)


def _failure(
    *,
    tool_call_id: str,
    source: str,
    query: dict[str, Any],
    time_range: TimeRange,
    code: str,
    message: str,
    retryable: bool,
) -> ToolResult:
    return ToolResult(
        tool_call_id=tool_call_id,
        source=source,
        query=query,
        time_range=time_range,
        collected_at=datetime.now(UTC),
        error=ToolError(code=code, message=message, retryable=retryable),
    )


def parse_series(payload: dict[str, Any], *, max_points: int, max_series: int) -> tuple[list, bool]:
    """把 Prometheus 结果压成按序列的点列表，超限时保留最近的采样。"""
    data = payload["data"]
    result_type = data["resultType"]
    result = data["result"]
    if result_type == "scalar":
        values = [result]
        entries = [{"metric": {}, "values": values}]
    elif result_type in {"matrix", "vector"}:
        entries = result
    else:
        raise ValueError(f"不支持的结果类型：{result_type}")

    truncated = len(entries) > max_series
    series = []
    for entry in entries[:max_series]:
        if result_type == "matrix":
            points = entry["values"]
        elif result_type == "vector":
            points = [entry["value"]]
        else:
            points = entry["values"]
        if len(points) > max_points:
            truncated = True
            points = points[-max_points:]
        series.append(
            {
                "labels": entry.get("metric", {}),
                "points": [[float(timestamp), _number(value)] for timestamp, value in points],
            }
        )
    return series, truncated


class PrometheusClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        max_points: int = 2000,
        max_series: int = 200,
    ) -> None:
        self._client = client
        self._max_points = max_points
        self._max_series = max_series

    @classmethod
    def connect(
        cls,
        base_url: str,
        *,
        timeout_seconds: float = 10.0,
        max_points: int = 2000,
        max_series: int = 200,
    ) -> Self:
        client = httpx.AsyncClient(base_url=base_url, timeout=timeout_seconds)
        return cls(client, max_points=max_points, max_series=max_series)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._client.aclose()

    async def query_range(
        self,
        context: ToolContext,
        expression: str,
        time_range: TimeRange,
        *,
        step_seconds: float,
    ) -> ToolResult:
        tool_call_id = uuid.uuid4().hex
        query = {"expression": expression, "step_seconds": step_seconds}
        reason = context.abort_reason()
        if reason is not None:
            return _failure(
                tool_call_id=tool_call_id,
                source="prometheus",
                query=query,
                time_range=time_range,
                code=reason,
                message="调用前已取消或超过截止时间",
                retryable=False,
            )

        params = {
            "query": expression,
            "start": time_range.start.timestamp(),
            "end": time_range.end.timestamp(),
            "step": f"{step_seconds}s",
        }
        try:
            response = await asyncio.wait_for(
                self._client.get("/api/v1/query_range", params=params),
                timeout=context.remaining(),
            )
        except TimeoutError:
            return _failure(
                tool_call_id=tool_call_id,
                source="prometheus",
                query=query,
                time_range=time_range,
                code="timeout",
                message=f"查询超过截止时间：{expression}",
                retryable=True,
            )
        except httpx.HTTPError as exc:
            return _failure(
                tool_call_id=tool_call_id,
                source="prometheus",
                query=query,
                time_range=time_range,
                code="unavailable",
                message=f"连接失败：{exc.__class__.__name__}",
                retryable=True,
            )

        if response.status_code in {401, 403}:
            return _failure(
                tool_call_id=tool_call_id,
                source="prometheus",
                query=query,
                time_range=time_range,
                code="auth",
                message=f"Prometheus 拒绝访问：HTTP {response.status_code}",
                retryable=False,
            )
        if response.status_code >= 400:
            return _failure(
                tool_call_id=tool_call_id,
                source="prometheus",
                query=query,
                time_range=time_range,
                code="bad_response",
                message=f"Prometheus 返回 HTTP {response.status_code}",
                retryable=response.status_code >= 500,
            )

        try:
            payload = response.json()
        except ValueError:
            return _failure(
                tool_call_id=tool_call_id,
                source="prometheus",
                query=query,
                time_range=time_range,
                code="bad_response",
                message="Prometheus 响应不是 JSON",
                retryable=False,
            )
        if payload.get("status") != "success":
            return _failure(
                tool_call_id=tool_call_id,
                source="prometheus",
                query=query,
                time_range=time_range,
                code="query_error",
                message=str(payload.get("error") or "查询失败"),
                retryable=False,
            )

        try:
            series, truncated = parse_series(
                payload, max_points=self._max_points, max_series=self._max_series
            )
        except (KeyError, TypeError, ValueError) as exc:
            return _failure(
                tool_call_id=tool_call_id,
                source="prometheus",
                query=query,
                time_range=time_range,
                code="bad_response",
                message=f"无法解析查询结果：{exc}",
                retryable=False,
            )

        raw = context.persist_raw(tool_call_id, payload)
        return ToolResult(
            tool_call_id=tool_call_id,
            source="prometheus",
            query=query,
            time_range=time_range,
            collected_at=datetime.now(UTC),
            data={"result_type": payload["data"]["resultType"], "series": series},
            raw_ref=raw.ref,
            truncated=truncated,
        )
