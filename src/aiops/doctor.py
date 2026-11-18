"""以只读请求检查本机 Vigil 所依赖的服务。"""

import asyncio
from collections.abc import Awaitable, Callable

import httpx
from kubernetes import client, config

from aiops.config import Settings
from aiops.db import Store, migration_files


def kube_configuration(settings: Settings) -> client.Configuration:
    kube = client.Configuration()
    if settings.kubeconfig is None:
        try:
            config.load_incluster_config(client_configuration=kube)
        except config.ConfigException:
            config.load_kube_config(context=settings.kube_context, client_configuration=kube)
    else:
        config.load_kube_config(
            config_file=str(settings.kubeconfig),
            context=settings.kube_context,
            client_configuration=kube,
        )
    return kube


def mutating_webhook_present(settings: Settings) -> bool:
    """按规则匹配 Deployment UPDATE；选择器无法确认时按可能匹配告警。"""
    with client.ApiClient(kube_configuration(settings)) as api:
        configs = client.AdmissionregistrationV1Api(api).list_mutating_webhook_configuration(
            _request_timeout=(settings.tool_timeout_seconds, settings.tool_timeout_seconds),
        )
    return any(
        rule.scope != "Cluster"
        and bool({"UPDATE", "*"} & set(rule.operations or []))
        and bool({"apps", "*"} & set(rule.api_groups or []))
        and bool({"v1", "*"} & set(rule.api_versions or []))
        and bool({"deployments", "*", "*/*"} & set(rule.resources or []))
        for configuration in configs.items
        for webhook in configuration.webhooks or []
        for rule in webhook.rules or []
    )


async def check_dependencies(settings: Settings) -> list[tuple[str, bool, str]]:
    async def database() -> bool:
        if not settings.database_url:
            return False
        async with Store.open(settings.database_url) as store:
            applied = await store.applied_migrations()
        return set(applied) == {path.stem for path in migration_files()}

    async def model() -> bool:
        if not settings.model_name:
            return False
        key = settings.model_api_key
        headers = {"Authorization": f"Bearer {key.get_secret_value()}"} if key else {}
        async with httpx.AsyncClient(timeout=settings.tool_timeout_seconds) as http:
            base = settings.model_base_url.rstrip("/")
            response = await http.get(f"{base}/models", headers=headers)
            if response.status_code == 404 and not base.endswith("/v1"):
                response = await http.get(f"{base}/v1/models", headers=headers)
            response.raise_for_status()
        return isinstance(response.json().get("data"), list)

    def cluster() -> bool:
        kube = kube_configuration(settings)
        with client.ApiClient(kube) as api:
            client.CoreV1Api(api).list_namespaced_service(
                settings.namespace,
                limit=1,
                _request_timeout=(settings.tool_timeout_seconds, settings.tool_timeout_seconds),
            )
        return True

    async def prometheus() -> bool:
        async with httpx.AsyncClient(timeout=settings.tool_timeout_seconds) as http:
            url = settings.prometheus_url.rstrip("/")
            response = await http.get(f"{url}/-/ready")
            return response.is_success

    async def probe() -> bool:
        async with httpx.AsyncClient(timeout=settings.tool_timeout_seconds) as http:
            response = await http.get(
                f"{settings.prometheus_url.rstrip('/')}/api/v1/query",
                params={"query": settings.verify_probe_query},
            )
            response.raise_for_status()
        data = response.json()["data"]
        return data["resultType"] == "vector" and len(data["result"]) == 1

    async def admission() -> bool:
        return not await asyncio.to_thread(mutating_webhook_present, settings)

    async def check(
        name: str, probe: Callable[[], Awaitable[bool]], hint: str
    ) -> tuple[str, bool, str]:
        try:
            ok = await probe()
        except Exception:  # 自检输出修复提示，避免依赖报错回显连接串或凭据。
            ok = False
        return name, ok, hint

    return list(
        await asyncio.gather(
            check("数据库", database, "检查连接设置，运行 uv run aiops dev db migrate。"),
            check("模型网关", model, "检查 VIGIL_MODEL_BASE_URL 和 VIGIL_MODEL_NAME。"),
            check(
                "实验集群",
                lambda: asyncio.to_thread(cluster),
                "检查集群与 VIGIL_KUBECONFIG、VIGIL_KUBE_CONTEXT、VIGIL_NAMESPACE。",
            ),
            check("Prometheus", prometheus, "检查 VIGIL_PROMETHEUS_URL 和端口转发。"),
            check("业务探针", probe, "检查 VIGIL_VERIFY_PROBE_QUERY 是否恰好返回一条序列。"),
            check("Deployment 准入", admission, "检查匹配 Deployment 更新的可变更准入规则。"),
        )
    )
