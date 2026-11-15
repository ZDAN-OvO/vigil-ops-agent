"""LangChain 接入关卡：用真实网关与真实 PostgreSQL 验证四件事。

1. 网关原生 tool calling 可用；
2. ToolMessage 往返后模型能给出终态内容；
3. 显式 `ToolStrategy` 的结构化终态可解析；
4. 官方 `AsyncPostgresSaver` 能把进度写进实验 PostgreSQL 并按 thread_id 读回。

失败时以非零退出码结束，并打印具体检查项。本脚本只做最小探测，不写集群、
不创建调查，也不会把密钥写进输出。
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from typing import Any

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from pydantic import BaseModel, ConfigDict, Field

from aiops.config import Settings


class GateReport(BaseModel):
    """关卡的结构化终态样例。"""

    model_config = ConfigDict(extra="forbid")

    observation: str = Field(min_length=1)
    conclusion: str = Field(min_length=1)


@tool
def echo_probe(text: str) -> str:
    """回显一段文本，用来确认工具往返。"""
    return f"echo:{text}"


def _api_key(settings: Settings) -> str:
    """本地网关不校验密钥；OpenAI SDK 仍要求非空，所以空值用占位串。"""

    if settings.model_api_key is None:
        return "sk-vigil-local-no-auth"
    value = settings.model_api_key.get_secret_value().strip()
    return value or "sk-vigil-local-no-auth"


def _model(settings: Settings) -> ChatOpenAI:
    if settings.model_name is None:
        raise SystemExit("未配置 VIGIL_MODEL_NAME")
    return ChatOpenAI(
        model=settings.model_name,
        base_url=settings.model_base_url,
        api_key=_api_key(settings),
        timeout=settings.model_timeout_seconds,
        # 关卡要求关闭 provider 隐藏重试：重试由预算中间件统一管理。
        max_retries=0,
        temperature=0,
    )


async def _tool_calling(model: ChatOpenAI) -> dict[str, Any]:
    bound = model.bind_tools([echo_probe])
    reply = await bound.ainvoke(
        [HumanMessage("调用 echo_probe，参数 text=magnet-ok。只调用工具，不要解释。")]
    )
    calls = getattr(reply, "tool_calls", None) or []
    if not calls:
        raise RuntimeError(f"网关没有返回 tool_calls：{reply!r}")
    if calls[0]["name"] != "echo_probe":
        raise RuntimeError(f"工具名不符：{calls[0]['name']}")
    tool_message = ToolMessage(
        content=str(echo_probe.invoke(calls[0]["args"])),
        tool_call_id=calls[0]["id"],
    )
    final = await bound.ainvoke(
        [
            HumanMessage("调用 echo_probe，参数 text=magnet-ok。只调用工具，不要解释。"),
            AIMessage(content=reply.content or "", tool_calls=calls),
            tool_message,
        ]
    )
    text = str(final.content or "")
    if not text.strip():
        raise RuntimeError("ToolMessage 往返后没有终态内容")
    return {
        "tool": calls[0]["name"],
        "arguments": calls[0]["args"],
        "tool_output": tool_message.content,
        "final_text": text[:200],
        "model": final.response_metadata.get("model_name")
        or final.response_metadata.get("model")
        or "unknown",
    }


async def _structured_output(model: ChatOpenAI) -> dict[str, Any]:
    agent = create_agent(
        model,
        tools=[echo_probe],
        system_prompt="只调用给定工具；需要结束时用结构化终态回答。",
        response_format=ToolStrategy(GateReport),
    )
    result = await agent.ainvoke(
        {
            "messages": [
                HumanMessage(
                    "先用 echo_probe 取一次 text=structured，然后给出结构化终态："
                    "observation 写工具返回值，conclusion 写一句话结论。"
                )
            ]
        },
        {"recursion_limit": 12},
    )
    structured = result.get("structured_response")
    if not isinstance(structured, GateReport):
        raise RuntimeError(f"结构化终态不是 GateReport：{structured!r}")
    return {"structured": structured.model_dump()}


async def _checkpoint(settings: Settings, model: ChatOpenAI) -> dict[str, Any]:
    dsn = settings.database_url
    if not dsn:
        raise RuntimeError("未配置 VIGIL_DATABASE_URL")
    thread_id = f"gate-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    agent = create_agent(
        model,
        tools=[echo_probe],
        system_prompt="只调用给定工具；需要结束时给出结构化终态。",
        response_format=ToolStrategy(GateReport),
        checkpointer=None,
    )
    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
        await saver.setup()
        with_checkpointer = agent.with_config({"recursion_limit": 12})
        # 只在本次关卡里替换 checkpointer，避免影响其它线程。
        graph = create_agent(
            model,
            tools=[echo_probe],
            system_prompt="只调用给定工具；需要结束时给出结构化终态。",
            response_format=ToolStrategy(GateReport),
            checkpointer=saver,
        )
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 12}
        await graph.ainvoke(
            {
                "messages": [
                    HumanMessage("调用一次 echo_probe（text=checkpoint），再给出结构化终态。")
                ]
            },
            config,
        )
        state = await graph.aget_state(config)
        messages = state.values.get("messages") or []
    if not messages:
        raise RuntimeError("checkpoint 里读不到消息")
    del with_checkpointer
    return {
        "thread_id": thread_id,
        "messages": len(messages),
        "has_structured_response": bool(state.values.get("structured_response")),
    }


async def main() -> int:
    settings = Settings()
    model = _model(settings)
    report: dict[str, Any] = {"base_url": settings.model_base_url, "checks": {}}
    failures: list[str] = []
    for name, factory in (
        ("tool_calling", lambda: _tool_calling(model)),
        ("structured_output", lambda: _structured_output(model)),
        ("checkpoint", lambda: _checkpoint(settings, model)),
    ):
        try:
            report["checks"][name] = {"ok": True, "detail": await factory()}
        except Exception as exc:  # noqa: BLE001 - 关卡要把失败项全部列出来
            report["checks"][name] = {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
            failures.append(name)
    report["ok"] = not failures
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
