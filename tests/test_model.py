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


SLEEPS: list[float] = []


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """退避等待只记录不真睡，单测保持秒级。"""

    SLEEPS.clear()

    async def fake_sleep(seconds: float) -> None:
        SLEEPS.append(seconds)

    monkeypatch.setattr("aiops.model._sleep", fake_sleep)


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


def test_backoff_grows_and_is_capped():
    from aiops.model import RETRY_MAX_SECONDS, backoff_seconds

    waits = [backoff_seconds(n) for n in (1, 2, 3, 6, 20)]
    assert waits[0] >= 0.5
    assert waits[1] > waits[0] * 0.9
    assert waits[3] <= RETRY_MAX_SECONDS * 1.25
    assert waits[4] <= RETRY_MAX_SECONDS * 1.25


def test_retry_waits_before_retrying_instead_of_hammering():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(503, json={"error": "overloaded"})
        return httpx.Response(200, json=completion('{"conclusion": "坏发布"}'))

    call = asyncio.run(
        make_client(handler, max_retries=3).complete_json(
            [{"role": "user", "content": "调查"}], Answer
        )
    )

    assert call.attempts == 3
    assert len(SLEEPS) == 2
    assert all(seconds > 0 for seconds in SLEEPS)


def test_timeout_is_retried_and_reported_as_timeout():
    def handler(request):
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(ModelError) as caught:
        asyncio.run(
            make_client(handler, max_retries=1).complete_json(
                [{"role": "user", "content": "调查"}], Answer
            )
        )

    assert caught.value.code == "exhausted"
    assert caught.value.attempts == 2
    assert "请求超时" in str(caught.value)
    assert len(SLEEPS) == 2
