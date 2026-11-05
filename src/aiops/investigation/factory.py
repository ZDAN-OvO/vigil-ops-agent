"""按配置装配一次调查运行时；CLI 与 webhook 共用同一套装配。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiops.actions.executor import ActionExecutor
from aiops.actions.restart_pod import PodRestartAdapter
from aiops.actions.rollout_undo import RolloutUndoAdapter
from aiops.config import Settings
from aiops.contracts import Incident, Investigation
from aiops.db import Store
from aiops.investigation.runtime import InvestigationRuntime
from aiops.investigation.toolbox import Toolbox
from aiops.model import ModelClient
from aiops.tools.kubernetes import KubernetesRead
from aiops.tools.prometheus import PrometheusClient


@asynccontextmanager
async def open_runtime(
    *,
    settings: Settings,
    store: Store,
    dsn: str,
    incident: Incident,
    investigation: Investigation,
) -> AsyncIterator[InvestigationRuntime]:
    if settings.model_name is None:
        raise ValueError("未配置 VIGIL_MODEL_NAME")
    async with PrometheusClient.connect(
        settings.prometheus_url,
        timeout_seconds=settings.tool_timeout_seconds,
        max_points=settings.tool_max_points,
        max_series=settings.tool_max_series,
    ) as prometheus:
        toolbox = Toolbox(
            prometheus,
            KubernetesRead(
                kubeconfig=settings.kubeconfig,
                context_name=settings.kube_context,
                max_items=settings.tool_max_items,
            ),
        )
        executor = ActionExecutor(
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
        )
        async with ModelClient.connect(
            settings.model_base_url,
            model=settings.model_name,
            api_key=settings.model_api_key,
            timeout_seconds=settings.model_timeout_seconds,
            max_retries=settings.model_max_retries,
        ) as model:
            yield InvestigationRuntime(
                store=store,
                toolbox=toolbox,
                model=model,
                dsn=dsn,
                incident=incident,
                investigation=investigation,
                observation_window=incident.time_range,
                runtime_path=settings.runtime_path,
                proposer=executor,
                tool_timeout_seconds=settings.tool_timeout_seconds,
                max_observation_chars=settings.investigation_max_observation_chars,
            )
