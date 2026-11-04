"""OpenAI 兼容模型客户端：结构化输出在客户端二次校验，重试有上限。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Self

import httpx
from pydantic import BaseModel, SecretStr


@dataclass(frozen=True)
class ModelCall[T: BaseModel]:
    value: T
    model: str
    attempts: int
    usage: dict[str, int] = field(default_factory=dict)


class ModelError(RuntimeError):
    def __init__(self, code: str, message: str, *, attempts: int) -> None:
        super().__init__(message)
        self.code = code
        self.attempts = attempts


RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class ModelClient:
    """只走 /v1/chat/completions；该端点在本项目网关上强制 response_format。"""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        model: str,
        api_key: SecretStr | None = None,
        max_retries: int = 2,
    ) -> None:
        self._client = client
        self._model = model
        self._api_key = api_key
        self._max_retries = max_retries

    @classmethod
    def connect(
        cls,
        base_url: str,
        *,
        model: str,
        api_key: SecretStr | None = None,
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
    ) -> Self:
        return cls(
            httpx.AsyncClient(base_url=base_url, timeout=timeout_seconds),
            model=model,
            api_key=api_key,
            max_retries=max_retries,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key.get_secret_value()}"
        return headers

    async def complete_json[T: BaseModel](
        self,
        messages: list[dict[str, str]],
        response_model: type[T],
        *,
        schema_name: str = "response",
        temperature: float | None = None,
    ) -> ModelCall[T]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "schema": response_model.model_json_schema(),
                    "strict": True,
                },
            },
        }
        if temperature is not None:
            payload["temperature"] = temperature

        attempt_messages = list(messages)
        last_error = "unknown"
        for attempt in range(1, self._max_retries + 2):
            try:
                response = await self._client.post(
                    "/v1/chat/completions", json={**payload, "messages": attempt_messages}
                )
            except httpx.HTTPError as exc:
                last_error = f"连接失败：{exc.__class__.__name__}"
                continue

            if response.status_code in {401, 403}:
                raise ModelError(
                    "auth", f"模型网关拒绝访问：HTTP {response.status_code}", attempts=attempt
                )
            if response.status_code >= 400:
                last_error = f"模型网关返回 HTTP {response.status_code}"
                if response.status_code in RETRYABLE_STATUS:
                    continue
                raise ModelError("bad_request", last_error, attempts=attempt)

            content: Any = None
            try:
                body = response.json()
                content = body["choices"][0]["message"]["content"]
                value = response_model.model_validate_json(content)
            except (KeyError, TypeError, ValueError) as exc:
                last_error = f"输出不符合 schema：{exc}"
                attempt_messages = [
                    *attempt_messages,
                    {"role": "assistant", "content": str(content or "")[:2000]},
                    {
                        "role": "user",
                        "content": "上一条输出不符合约定 schema，请只返回符合 schema 的 JSON。",
                    },
                ]
                continue

            usage = body.get("usage") or {}
            return ModelCall(
                value=value,
                model=str(body.get("model") or self._model),
                attempts=attempt,
                usage={
                    key: int(usage[key])
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                    if usage.get(key) is not None
                },
            )

        raise ModelError("exhausted", last_error, attempts=self._max_retries + 1)


__all__ = ["ModelCall", "ModelClient", "ModelError"]
