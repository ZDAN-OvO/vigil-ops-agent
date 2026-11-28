"""按配置装配调查与会话运行所需的工具和执行器。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiops.actions.executor import ActionExecutor
from aiops.actions.remediation_runtime import RemediationRuntime
from aiops.actions.restart_pod import PodRestartAdapter
from aiops.actions.rollout_undo import RolloutUndoAdapter
from aiops.agent.conversation import ConversationDriver
from aiops.agent.engine import LangChainInvestigation
from aiops.config import Settings
from aiops.contracts import Incident, Investigation
from aiops.db import Store
from aiops.inspection.collector import InspectionCollector, plan_checks
from aiops.inspection.config import InspectionConfigError, load_manifest, load_sop, validate_timing
from aiops.inspection.runtime import InspectionHook, InspectionScheduler
from aiops.inspection.store import InspectionStore
from aiops.investigation.toolbox import Toolbox
from aiops.sessions.models import Lease
from aiops.sessions.projection import project_text
from aiops.tools.kubernetes import KubernetesRead
from aiops.tools.prometheus import PrometheusClient

_LOG = logging.getLogger(__name__)


def build_executor(
    settings: Settings,
    store: Store,
    prometheus: PrometheusClient,
    *,
    session_lease: Lease | None = None,
    session_turn_id: str | None = None,
    remediation: RemediationRuntime | None = None,
) -> ActionExecutor:
    web_url = getattr(settings, "web_url", None)
    return ActionExecutor(
        store=store,
        adapter=RolloutUndoAdapter(
            kubeconfig=settings.action_kubeconfig, context_name=settings.action_kube_context
        ),
        restart_adapter=PodRestartAdapter(
            kubeconfig=settings.action_kubeconfig, context_name=settings.action_kube_context
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
        session_lease=session_lease,
        session_turn_id=session_turn_id,
        web_url=str(web_url) if web_url is not None else None,
        remediation=remediation,
    )


@asynccontextmanager
async def _open_read_clients(
    settings: Settings,
) -> AsyncIterator[tuple[KubernetesRead, PrometheusClient]]:
    async with PrometheusClient.connect(
        settings.prometheus_url,
        timeout_seconds=settings.tool_timeout_seconds,
        max_points=settings.tool_max_points,
        max_series=settings.tool_max_series,
    ) as prometheus:
        yield (
            KubernetesRead(
                kubeconfig=settings.kubeconfig,
                context_name=settings.kube_context,
                max_items=settings.tool_max_items,
            ),
            prometheus,
        )


@asynccontextmanager
async def _open_io(
    settings: Settings, *, store: Store
) -> AsyncIterator[tuple[Toolbox, PrometheusClient]]:
    async with _open_read_clients(settings) as (kubernetes, prometheus):
        yield Toolbox(prometheus, kubernetes, store), prometheus


@asynccontextmanager
async def open_inspection(
    settings: Settings,
    *,
    store: Store,
    on_observations: InspectionHook | None = None,
    inspections: InspectionStore | None = None,
) -> AsyncIterator[InspectionScheduler | None]:
    if not settings.inspection_enabled:
        yield None
        return
    try:
        interval, timeout = validate_timing(settings)
        manifest = load_manifest(settings)
        sop = load_sop(settings.skills_path)
    except InspectionConfigError as exc:
        _LOG.error("巡检无法启动：%s", exc)
        yield None
        return
    async with _open_read_clients(settings) as (kubernetes, prometheus):
        inspections = inspections or InspectionStore(store)
        collector = InspectionCollector(
            kubernetes,
            prometheus,
            cluster=manifest.cluster,
            runtime_path=settings.runtime_path,
            tool_timeout_seconds=settings.tool_timeout_seconds,
            now=inspections.now,
        )
        yield InspectionScheduler(
            inspections,
            manifest.cluster,
            interval,
            timeout,
            sop,
            plan_checks(manifest, sop),
            collector.sample,
            on_observations,
        )


@asynccontextmanager
async def open_conversation(
    *,
    settings: Settings,
    store: Store,
    remediation: RemediationRuntime | None = None,
) -> AsyncIterator[ConversationDriver]:
    """使用已初始化的数据库装配 CLI 与本地服务共用的会话驱动。"""
    async with _open_io(settings, store=store) as (toolbox, prometheus):

        @asynccontextmanager
        async def executor_factory(*, lease: Lease, turn_id: str) -> AsyncIterator[ActionExecutor]:
            yield build_executor(
                settings,
                store,
                prometheus,
                session_lease=lease,
                session_turn_id=turn_id,
                remediation=remediation,
            )

        yield ConversationDriver(
            settings=settings,
            store=store,
            toolbox=toolbox,
            executor_factory=executor_factory,
            project_text=project_text,
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
    async with _open_io(settings, store=store) as (toolbox, prometheus):
        yield LangChainInvestigation(
            settings=settings,
            store=store,
            dsn=dsn,
            incident=incident,
            investigation=investigation,
            toolbox=toolbox,
            executor=build_executor(settings, store, prometheus),
            runtime_path=settings.runtime_path,
        )


__all__ = [
    "build_executor",
    "open_agent",
    "open_conversation",
    "open_executor",
    "open_inspection",
]
