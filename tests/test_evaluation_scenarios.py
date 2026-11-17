"""场景定义、脚本结论行与评估驱动的契约。"""

import os
import subprocess
from pathlib import Path

import pytest

from aiops.config import PROJECT_ROOT, Settings
from aiops.evaluation.scenarios import (
    SCENARIOS,
    VERBS,
    EvidenceCheck,
    ScenarioError,
    ScenarioSpec,
    SupportFact,
    get_scenario,
    parse_result,
    run_verb,
)

SETTINGS = Settings(_env_file=None)
SCENARIO_IDS = [spec.id for spec in SCENARIOS]


def _fake_spec(tmp_path: Path, body: str) -> ScenarioSpec:
    script = tmp_path / "repeat.sh"
    script.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    return ScenarioSpec(
        id="fake",
        name="假场景",
        namespace="vigil-experiment",
        service="fake",
        symptoms="假",
        script=script,
        truth="假",
    )


def test_every_scenario_uses_the_same_verbs():
    for spec in SCENARIOS:
        text = spec.script.read_text()
        usage = next(line for line in text.splitlines() if "用法：" in line)
        for verb in VERBS:
            assert verb in usage, f"{spec.id} 的用法行缺少动词 {verb}"
        assert "result.sh" in text, f"{spec.id} 没有引用统一的结论行"


def test_expected_action_must_be_allowed():
    for spec in SCENARIOS:
        if spec.expects_no_action:
            assert spec.allowed_kinds == ()
        else:
            assert spec.expected_action in spec.allowed_kinds
            assert spec.expected_target


def test_scenario_ids_are_unique_and_resolvable():
    assert len(set(SCENARIO_IDS)) == len(SCENARIO_IDS)
    for scenario_id in SCENARIO_IDS:
        assert get_scenario(scenario_id).id == scenario_id
    with pytest.raises(ScenarioError):
        get_scenario("nope")


def test_scenario_scripts_live_in_the_repo():
    for spec in SCENARIOS:
        assert spec.script.is_relative_to(PROJECT_ROOT)
        assert spec.script.is_file()


def test_parse_result_reads_the_last_line_only():
    output = "noise\nRESULT verb=inject ok=0 reason=bad\nmore\nRESULT verb=inject ok=1 k=v\n"
    assert parse_result(output) == {"verb": "inject", "ok": "1", "k": "v"}


def test_parse_result_without_result_line_is_none():
    assert parse_result("VERIFIED something\n") is None


def test_evidence_check_needs_both_tool_and_keyword():
    facts = [
        SupportFact("obs-1", "kubernetes.list_pods", "app=x", "no match here"),
        SupportFact("obs-2", "kubernetes.list_deployments", "app=x", "OOMKilled"),
    ]
    assert EvidenceCheck("a", ("kubernetes.list_pods",)).matched_by(facts)
    assert not EvidenceCheck("b", ("kubernetes.list_pods",), ("OOMKilled",)).matched_by(facts)
    assert not EvidenceCheck("c", ("kubernetes.list_events",)).matched_by(facts)


def test_run_verb_parses_a_successful_result(tmp_path):
    spec = _fake_spec(tmp_path, 'echo "RESULT verb=preflight ok=1 probe=1"\nexit 0\n')
    outcome = run_verb(spec, "preflight", settings=SETTINGS)
    assert outcome.ok is True
    assert outcome.facts["probe"] == "1"


def test_run_verb_treats_missing_result_line_as_failure(tmp_path):
    spec = _fake_spec(tmp_path, 'echo "nothing here"\nexit 0\n')
    outcome = run_verb(spec, "preflight", settings=SETTINGS)
    assert outcome.ok is False
    assert outcome.facts["reason"] == "missing_result_line"


def test_run_verb_treats_nonzero_exit_as_failure(tmp_path):
    spec = _fake_spec(tmp_path, 'echo "RESULT verb=verify ok=1"\nexit 3\n')
    outcome = run_verb(spec, "verify", settings=SETTINGS)
    assert outcome.ok is False


def test_run_verb_rejects_unknown_verb(tmp_path):
    spec = _fake_spec(tmp_path, "exit 0\n")
    with pytest.raises(ScenarioError):
        run_verb(spec, "destroy", settings=SETTINGS)


def test_run_verb_fails_when_the_script_is_missing(tmp_path):
    spec = _fake_spec(tmp_path, "exit 0\n")
    (tmp_path / "repeat.sh").unlink()
    with pytest.raises(ScenarioError):
        run_verb(spec, "preflight", settings=SETTINGS)


def test_scenario_scripts_are_shell_parseable():
    """语法错误要在测试里暴露，不能等到真跑实验时才发现。"""
    for spec in SCENARIOS:
        completed = subprocess.run(
            ["bash", "-n", str(spec.script)],
            capture_output=True,
            text=True,
            env=dict(os.environ),
            check=False,
        )
        assert completed.returncode == 0, f"{spec.id}: {completed.stderr}"
