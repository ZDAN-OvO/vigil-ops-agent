"""写入传输只能从专用配置及 API Server 已核实身份建连。"""

from functools import partial
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes import client, config

from aiops.actions.kubernetes_write import WRITE_TIMEOUT_SECONDS, action_client
from aiops.actions.remediation_policy import REMEDIATION_IDENTITY


def test_write_transport_requires_explicit_kubeconfig(monkeypatch, tmp_path):
    loader = Mock(side_effect=AssertionError("不应读取隐式配置"))
    monkeypatch.setattr(config, "load_kube_config", loader)
    monkeypatch.setattr(config, "load_incluster_config", loader)

    for kubeconfig in (None, tmp_path / "missing"):
        with pytest.raises(config.ConfigException, match="KUBECONFIG"):
            action_client(kubeconfig, None)

    loader.assert_not_called()


@pytest.mark.parametrize("actual", [REMEDIATION_IDENTITY, "admin", None])
def test_write_client_checks_actual_identity_before_returning(monkeypatch, tmp_path, actual):
    kubeconfig = tmp_path / "restricted.yaml"
    kubeconfig.touch()
    loader = Mock()
    monkeypatch.setattr(config, "load_kube_config", loader)
    api_type = client.ApiClient
    close = Mock()
    monkeypatch.setattr(api_type, "close", close)
    reviewer = Mock()

    def check_options(body, *, _request_timeout):
        assert isinstance(body, client.V1SelfSubjectReview)
        assert _request_timeout == WRITE_TIMEOUT_SECONDS
        request = reviewer.call_args.args[0].rest_client.pool_manager.request
        assert isinstance(request, partial)
        assert request.keywords == {"retries": False, "redirect": False}
        return SimpleNamespace(status=SimpleNamespace(user_info=SimpleNamespace(username=actual)))

    reviewer.return_value.create_self_subject_review.side_effect = check_options
    monkeypatch.setattr(client, "AuthenticationV1Api", reviewer)
    if actual == REMEDIATION_IDENTITY:
        api = action_client(kubeconfig, "restricted")
        assert isinstance(api, api_type)
        close.assert_not_called()
    else:
        with pytest.raises(config.ConfigException, match="专用修复身份"):
            action_client(kubeconfig, "restricted")
        close.assert_called_once()
    loader.assert_called_once()
    assert loader.call_args.kwargs["config_file"] == str(kubeconfig)
    assert loader.call_args.kwargs["context"] == "restricted"
    assert loader.call_args.kwargs["persist_config"] is False


def test_expired_write_credentials_stop_before_client_is_available(monkeypatch, tmp_path):
    kubeconfig = tmp_path / "restricted.yaml"
    kubeconfig.touch()
    loader = Mock()
    monkeypatch.setattr(config, "load_kube_config", loader)
    close = Mock()
    monkeypatch.setattr(client.ApiClient, "close", close)
    reviewer = Mock()
    reviewer.return_value.create_self_subject_review.side_effect = client.ApiException(status=401)
    monkeypatch.setattr(client, "AuthenticationV1Api", reviewer)

    with pytest.raises(client.ApiException) as failure:
        action_client(kubeconfig, None)

    assert failure.value.status == 401
    close.assert_called_once()
