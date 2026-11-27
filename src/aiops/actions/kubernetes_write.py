"""动作适配器使用专用身份，每个请求只交付一次 HTTP 发送。"""

from functools import partial
from pathlib import Path

from kubernetes import client, config

from aiops.actions.remediation_policy import REMEDIATION_IDENTITY

WRITE_TIMEOUT_SECONDS = 15


def action_client(kubeconfig: Path | None, context_name: str | None) -> client.ApiClient:
    if kubeconfig is None or not kubeconfig.is_file():
        raise config.ConfigException("动作写入需要显式配置 VIGIL_ACTION_KUBECONFIG")
    configuration = client.Configuration()
    config.load_kube_config(
        config_file=str(kubeconfig),
        context=context_name,
        client_configuration=configuration,
        persist_config=False,
    )
    api = client.ApiClient(configuration)
    pool = api.rest_client.pool_manager
    pool.request = partial(pool.request, retries=False, redirect=False)
    try:
        review = client.AuthenticationV1Api(api).create_self_subject_review(
            client.V1SelfSubjectReview(), _request_timeout=WRITE_TIMEOUT_SECONDS
        )
        if review.status is None or review.status.user_info is None:
            raise config.ConfigException("集群未返回动作凭据身份")
        if review.status.user_info.username != REMEDIATION_IDENTITY:
            raise config.ConfigException("动作凭据不是专用修复身份")
    except Exception:
        api.close()
        raise
    return api
