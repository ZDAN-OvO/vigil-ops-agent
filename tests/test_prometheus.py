"""Prometheus 适配器：空结果、超限与失败必须能区分。"""

import asyncio
import hashlib
import json

import httpx

from aiops.contracts import TimeRange
from aiops.tools.base import ToolContext
from aiops.tools.prometheus import PrometheusClient

WINDOW = TimeRange(
    start="2026-11-04T09:00:00Z",
    end="2026-11-04T09:05:00Z",
)


def make_context(tmp_path, *, timeout_seconds=5.0):
    return ToolContext.create(
        investigation_id="inv-test",
        cluster="vigil-lab",
        namespace="vigil-shop",
        runtime_path=tmp_path,
        timeout_seconds=timeout_seconds,
    )


def make_client(handler, **limits):
    return PrometheusClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://prometheus"),
        **limits,
    )


def matrix(points):
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"__name__": "probe_success", "instance": "frontend"},
                    "values": [[str(ts), value] for ts, value in points],
                }
            ],
        },
    }


def test_successful_query_keeps_raw_response(tmp_path):
    payload = matrix([(1, "1"), (2, "0")])
    client = make_client(lambda request: httpx.Response(200, json=payload))

    result = asyncio.run(
        client.query_range(make_context(tmp_path), "probe_success", WINDOW, step_seconds=15)
    )

    assert result.error is None
    assert result.data["series"][0]["points"] == [[1.0, 1.0], [2.0, 0.0]]
    assert result.raw_ref is not None
    raw_file = tmp_path / result.raw_ref
    assert hashlib.sha256(raw_file.read_bytes()).hexdigest()
    assert json.loads(raw_file.read_text())["status"] == "success"


def test_empty_result_is_not_a_failure(tmp_path):
    payload = {"status": "success", "data": {"resultType": "matrix", "result": []}}
    client = make_client(lambda request: httpx.Response(200, json=payload))

    result = asyncio.run(
        client.query_range(make_context(tmp_path), "absent_metric", WINDOW, step_seconds=15)
    )

    assert result.error is None
    assert result.data["series"] == []


def test_truncation_keeps_most_recent_points(tmp_path):
    payload = matrix([(1, "1"), (2, "0"), (3, "1")])
    client = make_client(lambda request: httpx.Response(200, json=payload), max_points=2)

    result = asyncio.run(
        client.query_range(make_context(tmp_path), "probe_success", WINDOW, step_seconds=15)
    )

    assert result.truncated is True
    assert result.data["series"][0]["points"] == [[2.0, 0.0], [3.0, 1.0]]


def test_auth_failure_is_reported_not_retried(tmp_path):
    client = make_client(lambda request: httpx.Response(403, text="forbidden"))

    result = asyncio.run(
        client.query_range(make_context(tmp_path), "probe_success", WINDOW, step_seconds=15)
    )

    assert result.data is None
    assert result.error is not None
    assert result.error.code == "auth"
    assert result.error.retryable is False


def test_expired_context_does_not_call_prometheus(tmp_path):
    called = False

    def handler(request):
        nonlocal called
        called = True
        return httpx.Response(200, json=matrix([]))

    client = make_client(handler)
    result = asyncio.run(
        client.query_range(make_context(tmp_path, timeout_seconds=0), "up", WINDOW, step_seconds=15)
    )

    assert called is False
    assert result.error is not None
    assert result.error.code == "timeout"
