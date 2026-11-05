"""A01 脚本的删除保护：读不到归属或归属不符时，不能先删资源。"""

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "faults" / "a01-pod-rebuild" / "repeat.sh"


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
