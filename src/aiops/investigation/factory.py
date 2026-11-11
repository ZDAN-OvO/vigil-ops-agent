"""按配置装配一次单 Agent 调查；CLI 与 webhook 共用同一套装配。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiops.actions.executor import ActionExecutor
from aiops.actions.restart_pod import PodRestartAdapter
from aiops.actions.rollout_undo import RolloutUndoAdapter
from aiops.agent.engine import LangChainInvestigation
from aiops.config import Settings
from aiops.contracts import Incident, Investigation
from aiops.db import Store
from aiops.investigation.baseline import RulesBaseline
from aiops.investigation.toolbox import Toolbox
from aiops.model import ModelClient
from aiops.tools.kubernetes import KubernetesRead
from aiops.tools.prometheus import PrometheusClient


def build_executor(
    settings: Settings, store: Store, prometheus: PrometheusClient
) -> ActionExecutor:
    return ActionExecutor(
        store=store,
        adapter=RolloutUndoAdapter(
            kubeconfig=settings.kubeconfig, context_name=settings.kube_context
        ),
        restart_adapter=PodRestartAdapter(
            kubeconfig=settings.kubeconfig, context_name=settings.kube_context
        ),
        prometheus=prometheus,
        runtime_path=settings.runtime_path,
        cluster=settings.cluster,
        namespace=settings.namespace,
        rollout_timeout_seconds=settings.action_rollout_timeout_seconds,
        verify_seconds=settings.action_verify_seconds,
        sample_seconds=settings.action_sample_seconds,
        approval_ttl_seconds=settings.action_approval_ttl_seconds,
        io_timeout_seconds=settings.tool_timeout_seconds,
    )


@asynccontextmanager
async def _open_io(
    settings: Settings, *, store: Store
) -> AsyncIterator[tuple[Toolbox, ActionExecutor]]:
    """Agent 与规则基线共用同一只读工具和同一个动作执行器。"""
    async with PrometheusClient.connect(
        settings.prometheus_url,
        timeout_seconds=settings.tool_timeout_seconds,
        max_points=settings.tool_max_points,
        max_series=settings.tool_max_series,
    ) as prometheus:
        yield (
            Toolbox(
                prometheus,
                KubernetesRead(
                    kubeconfig=settings.kubeconfig,
                    context_name=settings.kube_context,
                    max_items=settings.tool_max_items,
                ),
                store,
            ),
            build_executor(settings, store, prometheus),
        )


@asynccontextmanager
async def open_executor(settings: Settings, *, store: Store) -> AsyncIterator[ActionExecutor]:
    """单独打开动作执行器：给 act / approve / resume 等旧命令使用。"""
    async with PrometheusClient.connect(
        settings.prometheus_url, timeout_seconds=settings.tool_timeout_seconds
    ) as prometheus:
        yield build_executor(settings, store, prometheus)


@asynccontextmanager
async def open_agent(
    *,
    settings: Settings,
    store: Store,
    dsn: str,
    incident: Incident,
    investigation: Investigation,
) -> AsyncIterator[LangChainInvestigation]:
    if settings.model_name is None:
        raise ValueError("未配置 VIGIL_MODEL_NAME")
    async with _open_io(settings, store=store) as (toolbox, executor):
        yield LangChainInvestigation(
            settings=settings,
            store=store,
            dsn=dsn,
            incident=incident,
            investigation=investigation,
            toolbox=toolbox,
            executor=executor,
            runtime_path=settings.runtime_path,
        )


@asynccontextmanager
async def open_baseline(
    *,
    settings: Settings,
    store: Store,
    incident: Incident,
    investigation: Investigation,
) -> AsyncIterator[RulesBaseline]:
    async with _open_io(settings, store=store) as (toolbox, executor):
        yield RulesBaseline(
            store=store,
            toolbox=toolbox,
            incident=incident,
            investigation=investigation,
            observation_window=incident.time_range,
            runtime_path=settings.runtime_path,
            proposer=executor,
            tool_timeout_seconds=settings.tool_timeout_seconds,
            max_observation_chars=settings.investigation_max_observation_chars,
        )


__all__ = [
    "ModelClient",
    "build_executor",
    "open_agent",
    "open_baseline",
    "open_executor",
]
