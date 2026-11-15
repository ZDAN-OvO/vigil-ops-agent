"""可选追踪接入：LangSmith 与 Langfuse 默认关闭。

关闭时不设置任何环境变量、也不注册回调，业务行为与未接入时一致。
"""

from __future__ import annotations

import os
from typing import Any

from loguru import logger

from aiops.config import Settings


def configure_tracing(settings: Settings) -> None:
    """把 Vigil 配置映射到各平台约定的环境变量。"""
    if settings.langsmith_tracing:
        key = settings.langsmith_api_key
        if key is None and not os.environ.get("LANGSMITH_API_KEY"):
            logger.warning("已启用 LangSmith，但未配置 API key；本次不追踪")
            return
        os.environ["LANGSMITH_TRACING"] = "true"
        os.environ.setdefault("LANGSMITH_PROJECT", settings.langsmith_project)
        if key is not None:
            os.environ.setdefault("LANGSMITH_API_KEY", key.get_secret_value())
    if settings.langfuse_enabled:
        if settings.langfuse_public_key is not None:
            os.environ.setdefault(
                "LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key.get_secret_value()
            )
        if settings.langfuse_secret_key is not None:
            os.environ.setdefault(
                "LANGFUSE_SECRET_KEY", settings.langfuse_secret_key.get_secret_value()
            )
        os.environ.setdefault("LANGFUSE_HOST", settings.langfuse_host)


def tracing_callbacks(settings: Settings) -> list[Any]:
    """Langfuse 需要显式回调；LangSmith 由 LangChain 环境变量自动接入。"""
    if not settings.langfuse_enabled:
        return []
    try:
        from langfuse.langchain import CallbackHandler
    except ImportError:  # pragma: no cover - 依赖缺失时不影响业务
        logger.warning("已启用 Langfuse，但未安装 langfuse；本次不追踪")
        return []
    return [CallbackHandler()]


__all__ = ["configure_tracing", "tracing_callbacks"]
