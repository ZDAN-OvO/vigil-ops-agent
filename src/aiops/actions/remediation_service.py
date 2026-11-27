"""组装受限客户端并驱动持久任务扫描。"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from aiops.actions.policy import load_policy
from aiops.actions.remediation_kube import RemediationKubeAdapter, RemediationKubeError
from aiops.actions.remediation_observation import RemediationMetrics
from aiops.actions.remediation_runtime import RemediationRuntime
from aiops.actions.remediation_store import RemediationStore
from aiops.actions.safety import ActionSafety
from aiops.config import Settings
from aiops.db import Store

_LOG = logging.getLogger(__name__)


@asynccontextmanager
async def open_remediation(
    settings: Settings,
    store: Store,
) -> AsyncIterator[RemediationRuntime | None]:
    policy = (
        load_policy(settings.remediation_config_dir) if settings.remediation_config_dir else None
    )
    if not settings.action_kubeconfig or policy is None or policy.error is not None:
        if settings.remediation_auto_enabled:
            _LOG.warning("自动处置已关闭，缺少有效策略和专用 Kubernetes 凭据")
        yield None
        return

    def registered(target) -> bool:
        return target.cluster == settings.cluster and (
            policy.service_for(target) is not None
            or (
                target.kind == "ConfigMap"
                and any(
                    service.namespace == target.namespace and field.config_map == target.name
                    for service in policy.services
                    for field in service.config_fields
                )
            )
        )

    def configured_consumer(workload, config_map) -> bool:
        service = policy.service_for(workload)
        return service is not None and any(
            field.config_map == config_map.name and field.load_signal_promql
            for field in service.config_fields
        )

    try:
        kube = await asyncio.to_thread(
            RemediationKubeAdapter.from_kubeconfig,
            kubeconfig=settings.action_kubeconfig,
            context_name=settings.action_kube_context,
            is_registered=registered,
            config_map_exclusive=configured_consumer,
        )
        await asyncio.to_thread(
            kube.require_admission,
            policy.version,
            exclusive_config=any(
                field.config_map for service in policy.services for field in service.config_fields
            ),
        )
    except RemediationKubeError as exc:
        _LOG.warning("集群写入关闭（%s）", exc.code)
        yield None
        return
    safety = ActionSafety(
        store,
        web_url=settings.web_url,
        approval_ttl_seconds=settings.action_approval_ttl_seconds,
    )
    async with httpx.AsyncClient(
        base_url=settings.prometheus_url,
        timeout=settings.tool_timeout_seconds,
    ) as client:
        yield RemediationRuntime(
            settings,
            RemediationStore(store, safety),
            policy,
            kube,
            RemediationMetrics(client),
        )


async def drive_remediation(runtime: RemediationRuntime, stop: asyncio.Event) -> None:
    running: dict[str, asyncio.Task] = {}

    async def run(name, operation, *args):
        try:
            await operation(*args)
        except Exception as exc:
            _LOG.error("后台处置 %s 暂无法继续（%s）", name, type(exc).__name__)

    try:
        while not stop.is_set():
            running = {key: task for key, task in running.items() if not task.done()}
            if "rules" not in running:
                running["rules"] = asyncio.create_task(run("rules", runtime.evaluate_one))
            try:
                due = await runtime.ledger.due()
                for proposal_id in due:
                    if proposal_id not in running:
                        running[proposal_id] = asyncio.create_task(
                            run(proposal_id, runtime.drive, proposal_id),
                        )
            except Exception as exc:
                _LOG.error("处置任务扫描失败（%s）", type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=1)
            except TimeoutError:
                pass
    finally:
        await asyncio.gather(*running.values())
