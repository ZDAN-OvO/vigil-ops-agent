"""动作适配器使用独立客户端，每个请求只交付一次 HTTP 发送。"""

from functools import partial
from pathlib import Path

from kubernetes import client, config

WRITE_TIMEOUT_SECONDS = 15


def action_client(kubeconfig: Path | None, context_name: str | None) -> client.ApiClient:
    configuration = client.Configuration()
    if kubeconfig is not None:
        config.load_kube_config(
            config_file=str(kubeconfig), context=context_name, client_configuration=configuration
        )
    else:
        try:
            config.load_incluster_config(client_configuration=configuration)
        except config.ConfigException:
            config.load_kube_config(context=context_name, client_configuration=configuration)
    api = client.ApiClient(configuration)
    pool = api.rest_client.pool_manager
    pool.request = partial(pool.request, retries=False, redirect=False)
    return api
