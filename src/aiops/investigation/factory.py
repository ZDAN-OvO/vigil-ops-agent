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
from aiops.investigation.rules import RulesInvestigator
from aiops.investigation.toolbox import Toolbox
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
async def open_runtime(
    *,
    settings: Settings,
    store: Store,
    dsn: str,
    incident: Incident,
    investigation: Investigation,
    arm: str = "agent",
) -> AsyncIterator[LangChainInvestigation | RulesInvestigator]:
    """按对照臂装配调查运行时；三条臂共用同一套工具与动作执行器。

    `agent`：现有运行时（Skill + 排查方法提示词）；`generic`：同一 create_agent
    循环但去掉 Skill 与方法论；`rules`：固定规则链，不调用模型。
    """
    async with _open_io(settings, store=store) as (toolbox, executor):
        if arm == "rules":
            yield RulesInvestigator(
                store=store,
                incident=incident,
                investigation=investigation,
                toolbox=toolbox,
                executor=executor,
                runtime_path=settings.runtime_path,
                max_observation_chars=settings.investigation_max_observation_chars,
                tool_timeout_seconds=settings.tool_timeout_seconds,
            )
            return
        if settings.model_name is None:
            raise ValueError("未配置 VIGIL_MODEL_NAME")
        yield LangChainInvestigation(
            settings=settings,
            store=store,
            dsn=dsn,
            incident=incident,
            investigation=investigation,
            toolbox=toolbox,
            executor=executor,
            runtime_path=settings.runtime_path,
            arm=arm,
        )


@asynccontextmanager
async def open_agent(
    *,
    settings: Settings,
    store: Store,
    dsn: str,
    incident: Incident,
    investigation: Investigation,
    arm: str = "agent",
) -> AsyncIterator[LangChainInvestigation | RulesInvestigator]:
    async with open_runtime(
        settings=settings,
        store=store,
        dsn=dsn,
        incident=incident,
        investigation=investigation,
        arm=arm,
    ) as runtime:
        yield runtime


__all__ = [
    "build_executor",
    "open_agent",
    "open_executor",
    "open_runtime",
]
