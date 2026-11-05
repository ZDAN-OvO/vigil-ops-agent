"""A01 脚本的删除保护：读不到归属或归属不符时，不能先删资源。"""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "faults" / "a01-pod-rebuild" / "repeat.sh"
V06_SCRIPT = Path(__file__).resolve().parents[1] / "faults" / "a01-pod-rebuild" / "v06.sh"


def run_cleanup(tmp_path, stub_body: str, **extra):
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(stub_body, encoding="utf-8")
    kubectl.chmod(0o755)
    env = {
        **os.environ,
        "KUBECTL": str(kubectl),
        "KUBECONFIG": str(tmp_path / "unused"),
        "LOG": str(tmp_path / "calls.log"),
        **extra,
    }
    result = subprocess.run(
        ["bash", str(SCRIPT), "cleanup"], env=env, capture_output=True, text=True
    )
    calls = (
        (tmp_path / "calls.log").read_text(encoding="utf-8")
        if (tmp_path / "calls.log").exists()
        else ""
    )
    return result, calls


def test_cleanup_refuses_when_namespace_owner_cannot_be_read(tmp_path):
    result, calls = run_cleanup(
        tmp_path,
        "#!/bin/sh\n"
        'echo "$@" >> "$LOG"\n'
        "echo 'Error from server (Forbidden): namespaces is forbidden' >&2\n"
        "exit 1\n",
    )

    assert result.returncode != 0
    assert "无法读取命名空间" in result.stdout + result.stderr
    assert "delete" not in calls


def test_cleanup_refuses_a_namespace_owned_by_someone_else(tmp_path):
    result, calls = run_cleanup(
        tmp_path,
        "#!/bin/sh\n"
        'echo "$@" >> "$LOG"\n'
        'case "$1 $2" in\n'
        '  "get namespace") printf "someone-else"; exit 0 ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n",
    )

    assert result.returncode != 0
    assert "不属于 A01" in result.stdout + result.stderr
    assert "delete" not in calls


def test_script_refuses_a_namespace_that_does_not_match_manifests(tmp_path):
    result, calls = run_cleanup(
        tmp_path,
        '#!/bin/sh\necho "$@" >> "$LOG"\nexit 0\n',
        NS="other-namespace",
    )

    assert result.returncode != 0
    assert "NS 必须与 manifests.yaml 一致" in result.stdout + result.stderr
    assert "delete" not in calls


def run_v06(tmp_path, stub_body: str, *args, uv_body=None, **extra):
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(stub_body, encoding="utf-8")
    kubectl.chmod(0o755)
    uv = tmp_path / "uv"
    uv.write_text(uv_body or '#!/bin/sh\necho "uv $@" >> "$LOG"\nexit 0\n', encoding="utf-8")
    uv.chmod(0o755)
    env = {
        **os.environ,
        "KUBECTL": str(kubectl),
        "KUBECONFIG": str(tmp_path / "unused"),
        "LOG": str(tmp_path / "calls.log"),
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        **extra,
    }
    result = subprocess.run(
        ["bash", str(V06_SCRIPT), *args], env=env, capture_output=True, text=True
    )
    calls = (
        (tmp_path / "calls.log").read_text(encoding="utf-8")
        if (tmp_path / "calls.log").exists()
        else ""
    )
    return result, calls


@pytest.mark.parametrize(
    "command", ["poison-on", "poison-off", "corrupt", "restart", "second-proposal"]
)
def test_v06_guarded_commands_refuse_when_namespace_owner_cannot_be_read(tmp_path, command):
    result, calls = run_v06(
        tmp_path,
        "#!/bin/sh\n"
        'echo "$@" >> "$LOG"\n'
        "echo 'Error from server (Forbidden): namespaces is forbidden' >&2\n"
        "exit 1\n",
        command,
    )

    assert result.returncode != 0
    assert "无法读取命名空间" in result.stdout + result.stderr
    assert "patch" not in calls
    assert "exec" not in calls
    assert "aiops act" not in calls


@pytest.mark.parametrize(
    "command", ["poison-on", "poison-off", "corrupt", "restart", "second-proposal"]
)
def test_v06_guarded_commands_refuse_a_namespace_owned_by_someone_else(tmp_path, command):
    result, calls = run_v06(
        tmp_path,
        "#!/bin/sh\n"
        'echo "$@" >> "$LOG"\n'
        'case "$1 $2" in\n'
        '  "get namespace") printf "someone-else"; exit 0 ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n",
        command,
    )

    assert result.returncode != 0
    assert "不属于 V06 实验" in result.stdout + result.stderr
    assert "patch" not in calls
    assert "exec" not in calls
    assert "aiops act" not in calls


def test_v06_refuses_a_context_mismatch(tmp_path):
    result, calls = run_v06(
        tmp_path,
        "#!/bin/sh\n"
        'echo "$@" >> "$LOG"\n'
        'case "$1 $2" in\n'
        '  "config current-context") printf "ctx-a"; exit 0 ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n",
        "poison-on",
        VIGIL_KUBE_CONTEXT="ctx-b",
    )

    assert result.returncode != 0
    assert "不一致" in result.stdout + result.stderr
    assert "patch" not in calls


def test_v06_uses_settings_context_for_the_guard(tmp_path):
    result, calls = run_v06(
        tmp_path,
        "#!/bin/sh\n"
        'echo "$@" >> "$LOG"\n'
        'case "$1 $2" in\n'
        '  "config current-context") printf "ctx-a"; exit 0 ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n",
        "poison-on",
        uv_body='#!/bin/sh\nprintf "ctx-b"\nexit 0\n',
    )

    assert result.returncode != 0
    assert "不一致" in result.stdout + result.stderr
    assert "patch" not in calls


def test_v06_fails_closed_when_settings_cannot_be_read(tmp_path):
    result, calls = run_v06(
        tmp_path,
        '#!/bin/sh\necho "$@" >> "$LOG"\nexit 0\n',
        "poison-on",
        uv_body="#!/bin/sh\nexit 1\n",
    )

    assert result.returncode != 0
    assert "无法读取 Settings" in result.stdout + result.stderr
    assert "patch" not in calls
    assert "aiops act" not in calls


def test_v06_reports_a_missing_namespace(tmp_path):
    result, calls = run_v06(
        tmp_path,
        "#!/bin/sh\n"
        'echo "$@" >> "$LOG"\n'
        "echo 'Error from server (NotFound): namespaces \"vigil-experiment\" not found' >&2\n"
        "exit 1\n",
        "poison-on",
    )

    assert result.returncode != 0
    assert "不存在" in result.stdout + result.stderr
    assert "patch" not in calls


def test_v06_poison_on_proceeds_for_the_owned_namespace(tmp_path):
    result, calls = run_v06(
        tmp_path,
        "#!/bin/sh\n"
        'echo "$@" >> "$LOG"\n'
        'case "$1 $2" in\n'
        '  "get namespace") printf "a01-pod-rebuild"; exit 0 ;;\n'
        '  "get pods") echo \'{"items": []}\'; exit 0 ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n",
        "poison-on",
    )

    assert result.returncode == 0
    assert "patch" in calls
