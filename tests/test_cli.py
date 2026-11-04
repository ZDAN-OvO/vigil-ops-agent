"""验证对外 CLI 契约，避免骨架把未实现能力报告为成功。"""

import json

from typer.testing import CliRunner

from aiops.cli import app

runner = CliRunner()


def test_help_does_not_require_external_services():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "status" in result.stdout


def test_status_json_reports_only_implemented_capabilities():
    result = runner.invoke(app, ["status", "--output", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["stage"] == "W0"
    assert payload["capabilities"]["investigation"] is True
    assert payload["capabilities"]["remediation"] is False
    assert payload["external_services_checked"] is False


def test_unimplemented_action_is_not_a_successful_noop():
    result = runner.invoke(app, ["act"])
    assert result.exit_code != 0
