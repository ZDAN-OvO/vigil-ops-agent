"""可选追踪默认关闭；启用时只设置约定环境变量。"""

import os

from aiops.config import Settings
from aiops.tracing import configure_tracing, tracing_callbacks


def test_tracing_is_disabled_by_default(monkeypatch):
    for key in ("LANGSMITH_TRACING", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(key, raising=False)
    settings = Settings(langsmith_tracing=False, langfuse_enabled=False)
    configure_tracing(settings)
    assert "LANGSMITH_TRACING" not in os.environ
    assert tracing_callbacks(settings) == []


def test_langsmith_toggle_sets_environment(monkeypatch):
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    settings = Settings(
        langsmith_tracing=True,
        langsmith_api_key="ls-key",
        langsmith_project="vigil-test",
        langfuse_enabled=False,
    )
    try:
        configure_tracing(settings)
        assert os.environ["LANGSMITH_TRACING"] == "true"
        assert os.environ["LANGSMITH_API_KEY"] == "ls-key"
        assert os.environ["LANGSMITH_PROJECT"] == "vigil-test"
    finally:
        for key in ("LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT"):
            os.environ.pop(key, None)
