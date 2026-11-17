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


def test_evidence_check_needs_both_tool_and_fact():
    oom_data = {
        "items": [
            {
                "name": "p",
                "containers": [{"last_terminated": {"reason": "OOMKilled"}}],
            }
        ]
    }
    facts = [
        SupportFact("obs-1", "kubernetes.list_pods", "app=x", "", data={"items": []}),
        SupportFact("obs-2", "kubernetes.list_deployments", "app=x", "", data=oom_data),
    ]
    assert EvidenceCheck("a", ("kubernetes.list_pods",)).matched_by(facts)
    assert not EvidenceCheck("b", ("kubernetes.list_pods",), fact="oom_killed").matched_by(facts)
    assert not EvidenceCheck("c", ("kubernetes.list_events",)).matched_by(facts)


def test_business_failure_needs_a_low_value_not_just_a_query_name():
    low = {"series": [{"labels": {}, "points": [[1.0, 0.0]]}]}
    high = {"series": [{"labels": {}, "points": [[1.0, 1.0]]}]}
    probe = SupportFact("obs-1", "prometheus.query_range", 'probe_success{job="x"}', "", data=low)
    ok = SupportFact("obs-2", "prometheus.query_range", 'probe_success{job="x"}', "", data=high)
    latency = SupportFact(
        "obs-3", "prometheus.query_range", 'probe_duration_seconds{job="x"}', "", data=low
    )
    check = EvidenceCheck("业务失败", ("prometheus.query_range",), fact="business_failure")
    assert check.matched_by([probe])
    assert not check.matched_by([ok])
    assert not check.matched_by([latency])


def test_latency_elevated_uses_the_value():
    slow = {"series": [{"labels": {}, "points": [[1.0, 0.42]]}]}
    fast = {"series": [{"labels": {}, "points": [[1.0, 0.007]]}]}
    check = EvidenceCheck("延迟事实", ("prometheus.query_range",), fact="latency_elevated")
    assert check.matched_by(
        [SupportFact("o", "prometheus.query_range", "probe_duration_seconds", "", data=slow)]
    )
    assert not check.matched_by(
        [SupportFact("o", "prometheus.query_range", "probe_duration_seconds", "", data=fast)]
    )


def test_http_5xx_reads_log_lines():
    data = {"items": [{"pod": "p", "lines": ['"GET /work HTTP/1.1" 500 24']}]}
    check = EvidenceCheck("业务 5xx", ("kubernetes.list_logs",), fact="http_5xx")
    assert check.matched_by([SupportFact("o", "kubernetes.list_logs", "app=x", "", data=data)])
    ok = {"items": [{"pod": "p", "lines": ['"GET /work HTTP/1.1" 200 2']}]}
    assert not check.matched_by([SupportFact("o", "kubernetes.list_logs", "app=x", "", data=ok)])


def test_unknown_fact_name_is_a_programming_error():
    with pytest.raises(ScenarioError):
        EvidenceCheck("x", ("kubernetes.list_pods",), fact="nope").matched_by([])


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


def test_derived_metric_expressions_do_not_count_as_raw_values():
    inverted = {"series": [{"labels": {}, "points": [[1.0, 0.0]]}]}
    check = EvidenceCheck("业务失败", ("prometheus.query_range",), fact="business_failure")
    assert not check.matched_by(
        [SupportFact("o", "prometheus.query_range", "1 - probe_success", "", data=inverted)]
    )
    assert check.matched_by(
        [
            SupportFact(
                "o",
                "prometheus.query_range",
                'probe_success{job="frontend-probe"}',
                "",
                data=inverted,
            )
        ]
    )
    for derived in (
        "probe_success == bool 0",
        "rate(probe_success[5m])",
        "count(probe_success)",
        "sum(probe_success)",
    ):
        assert not check.matched_by(
            [SupportFact("o", "prometheus.query_range", derived, "", data=inverted)]
        )
