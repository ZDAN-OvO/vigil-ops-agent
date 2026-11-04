"""模型客户端：结构化输出二次校验，失败重试有上限，不掩盖耗尽。"""

import asyncio
import json

import httpx
import pytest
from pydantic import BaseModel

from aiops.model import ModelClient, ModelError


class Answer(BaseModel):
    conclusion: str


def completion(content: str) -> dict:
    return {
        "model": "actual-model-1",
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
    }


def make_client(handler, *, max_retries=2):
    return ModelClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://gateway"),
        model="alias-model",
        max_retries=max_retries,
    )


def test_valid_output_records_actual_model_and_usage():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=completion('{"conclusion": "坏发布"}'))

    call = asyncio.run(
        make_client(handler).complete_json([{"role": "user", "content": "调查"}], Answer)
    )

    assert call.value.conclusion == "坏发布"
    assert call.model == "actual-model-1"
    assert call.attempts == 1
    assert call.usage["total_tokens"] == 15
    body = requests[0]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True


def test_invalid_output_is_repaired_once_then_accepted():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, json=completion("不是 JSON"))
        return httpx.Response(200, json=completion('{"conclusion": "坏发布"}'))

    call = asyncio.run(
        make_client(handler).complete_json([{"role": "user", "content": "调查"}], Answer)
    )

    assert call.attempts == 2
    assert len(requests) == 2
    assert requests[1]["messages"][-1]["role"] == "user"
    assert "schema" in requests[1]["messages"][-1]["content"]


def test_auth_failure_is_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, json={"error": "unauthorized"})

    with pytest.raises(ModelError) as caught:
        asyncio.run(
            make_client(handler).complete_json([{"role": "user", "content": "调查"}], Answer)
        )

    assert caught.value.code == "auth"
    assert len(calls) == 1


def test_transient_status_is_retried_within_limit():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json=completion('{"conclusion": "坏发布"}'))

    call = asyncio.run(
        make_client(handler, max_retries=1).complete_json(
            [{"role": "user", "content": "调查"}], Answer
        )
    )

    assert call.attempts == 2
    assert len(calls) == 2


def test_exhausted_retries_are_reported_not_hidden():
    def handler(request):
        return httpx.Response(200, json=completion("仍然不是 JSON"))

    with pytest.raises(ModelError) as caught:
        asyncio.run(
            make_client(handler, max_retries=1).complete_json(
                [{"role": "user", "content": "调查"}], Answer
            )
        )

    assert caught.value.code == "exhausted"
    assert caught.value.attempts == 2
