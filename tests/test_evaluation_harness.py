"""评估编排的纯函数部分：汇总、门槛与分批合并。"""

import asyncio

from aiops.evaluation.harness import (
    MIN_DIAGNOSIS_RATE,
    MIN_VALID_RUNS,
    compare,
    merge_records,
    render_markdown,
    summarize,
)


def _record(scenario: str, arm: str, index: int, *, valid=True, correct=True, violations=0):
    return {
        "run_id": f"run-{scenario}-{arm}-{index}",
        "scenario": scenario,
        "arm": arm,
        "index": index,
        "valid": valid,
        "environment_restored": True,
        "verdict": {
            "diagnosis_correct": correct,
            "confirmed": correct,
            "recovered_by_arm": False,
            "blocked_on_approval": False,
            "needs_review": [],
            "boundary_violations": ["x"] * violations,
        },
        "facts": {"cli": {"elapsed_seconds": 10.0 + index}},
    }


def test_invalid_runs_are_counted_but_not_scored():
    records = [
        _record("f01", "rules", 1, valid=False, correct=False),
        _record("f01", "rules", 2),
        _record("f01", "rules", 3),
    ]
    row = summarize(records)["rows"][0]
    assert row["runs"] == 3
    assert row["valid"] == 2
    assert row["diagnosis_correct"] == 2


def test_floor_needs_three_valid_runs_and_two_thirds_correct():
    two_valid = summarize([_record("f01", "rules", 1), _record("f01", "rules", 2)])["rows"][0]
    assert two_valid["valid"] == 2
    assert two_valid["meets_floor"] is False

    three = [_record("f01", "rules", i) for i in (1, 2, 3)]
    assert summarize(three)["rows"][0]["meets_floor"] is True

    # 恰好 2/3 命中下限；1/3 才不达标。
    borderline = [
        _record("f01", "rules", 1),
        _record("f01", "rules", 2),
        _record("f01", "rules", 3, correct=False),
    ]
    assert summarize(borderline)["rows"][0]["meets_floor"] is True
    failing = [
        _record("f01", "rules", 1),
        _record("f01", "rules", 2, correct=False),
        _record("f01", "rules", 3, correct=False),
    ]
    assert summarize(failing)["rows"][0]["meets_floor"] is False


def test_any_boundary_violation_blocks_the_floor():
    records = [_record("f01", "rules", i, violations=1 if i == 1 else 0) for i in (1, 2, 3)]
    row = summarize(records)["rows"][0]
    assert row["boundary_violations"] == 1
    assert row["meets_floor"] is False


def test_compare_refuses_a_conclusion_with_one_qualified_arm():
    records = [_record("f01", "rules", i) for i in (1, 2, 3)]
    notes = summarize(records)["comparison"]
    assert notes and "不构成对照结论" in notes[0]


def test_compare_says_equal_arms_are_not_a_gain():
    records = [_record("f01", "rules", i) for i in (1, 2, 3)]
    records += [_record("f01", "agent", i) for i in (1, 2, 3)]
    notes = compare(summarize(records)["rows"])
    assert "在可比较维度上相同" in notes[0]
    assert "不构成收益结论" in notes[0]


def test_compare_reports_dominance_when_one_arm_is_better_everywhere():
    records = [_record("f01", "rules", i) for i in (1, 2, 3)]
    records += [_record("f01", "agent", i, correct=(i <= 2)) for i in (1, 2, 3)]
    notes = compare(summarize(records)["rows"])
    assert any("rules 在可比较维度上不劣于 agent" in note for note in notes)


def test_compare_reports_mixed_strengths():
    # 规则臂 2/3 但很快；Agent 臂 3/3 但慢得多：各有强弱，不能宣称收益。
    fast = [_record("f01", "rules", i, correct=(i <= 2)) for i in (1, 2, 3)]
    slow = []
    for i in (1, 2, 3):
        item = _record("f01", "agent", i)
        item["facts"]["cli"]["elapsed_seconds"] = 100.0 + i
        slow.append(item)
    notes = compare(summarize(fast + slow)["rows"])
    assert any("各有强弱" in note for note in notes)


def test_median_ignores_zero_and_even_counts():
    records = [_record("f01", "rules", 1), _record("f01", "rules", 2)]
    row = summarize(records)["rows"][0]
    assert row["median_wall_seconds"] == 11.5


def test_merge_keeps_one_record_per_run_id_and_unions_batch_metadata():
    first = {
        "batch": {"date": "2026-11-18", "arms": ["rules"], "runs_per_cell": 3},
        "records": [_record("f01", "rules", 1)],
    }
    batch = {"date": "2026-11-19", "label": "第二批", "arms": ["agent"], "runs_per_cell": 1}
    merged_batch, records = merge_records(
        first, batch, [_record("f01", "agent", 1), _record("f01", "rules", 1)]
    )
    assert [item["index"] for item in records] == [1, 1]
    assert merged_batch["date"] == "2026-11-18"
    assert merged_batch["arms"] == ["agent", "rules"]
    assert merged_batch["runs_per_cell"] == 3
    assert len(records) == 2
    assert merged_batch["environment_restored"] is True


def test_render_markdown_states_thresholds_and_refuses_unsupported_claims():
    records = [_record("f01", "rules", i) for i in (1, 2, 3)]
    summary = summarize(records)
    text = render_markdown(
        {
            "date": "2026-11-18",
            "scenarios": ["f01"],
            "arms": ["rules"],
            "runs_per_cell": 3,
            "environment_restored": True,
        },
        summary,
    )
    assert f"至少 {MIN_VALID_RUNS} 次有效运行" in text
    assert f"{MIN_DIAGNOSIS_RATE:.2f}" in text
    assert "不构成对照结论" in text


def test_merge_reindexes_each_cell_by_start_time():
    first = {
        "batch": {"date": "2026-11-18"},
        "records": [
            {**_record("f01", "rules", 1), "run_id": "a", "started_at": "2026-11-18T01:00:00"},
            {**_record("f01", "rules", 2), "run_id": "b", "started_at": "2026-11-18T02:00:00"},
        ],
    }
    later = {**_record("f01", "rules", 1), "run_id": "c", "started_at": "2026-11-18T03:00:00"}
    _, records = merge_records(first, {"date": "2026-11-19"}, [later])
    assert [item["run_id"] for item in records] == ["a", "b", "c"]
    assert [item["index"] for item in records] == [1, 2, 3]


def test_missing_median_does_not_create_a_false_dominance():
    fast = [_record("f01", "rules", i) for i in (1, 2, 3)]
    for item in fast:
        item["facts"]["cli"]["elapsed_seconds"] = None
    slow = []
    for i in (1, 2, 3):
        item = _record("f01", "agent", i)
        item["facts"]["cli"]["elapsed_seconds"] = 100.0 + i
        slow.append(item)
    notes = compare(summarize(fast + slow)["rows"])
    assert any("耗时 缺失，未比较" in note for note in notes)
    assert not any("支持相对收益结论" in note for note in notes)


def _record_costs(scenario, arm, index, *, tools=10, models=5, approvals=0, **kw):
    record = _record(scenario, arm, index, **kw)
    record["facts"].update({"tool_calls": tools, "model_calls": models, "approvals": approvals})
    return record


def test_missing_elapsed_blocks_the_time_dimension():
    """有一次没记到耗时，就不能拿剩下的算中位——那会把慢样本丢掉。"""
    agent = [_record_costs("f01", "agent", i) for i in (1, 2, 3)]
    agent[0]["facts"]["cli"]["elapsed_seconds"] = 1.0
    agent[1]["facts"]["cli"]["elapsed_seconds"] = None
    agent[2]["facts"]["cli"]["elapsed_seconds"] = None
    rules = [_record_costs("f01", "rules", i, tools=10, models=5) for i in (1, 2, 3)]
    for item in rules:
        item["facts"]["cli"]["elapsed_seconds"] = 60.0
    row = summarize(agent + rules)
    agent_row = next(item for item in row["rows"] if item["arm"] == "agent")
    assert agent_row["elapsed_missing"] == 2
    assert "中位耗时 缺失" in "".join(row["comparison"])
    assert "不构成收益结论" in "".join(row["comparison"])


def test_cost_dimensions_can_veto_a_time_gain():
    """耗时更快但工具、模型、审批都更多，不算收益。"""
    agent = [_record_costs("f01", "agent", i) for i in (1, 2, 3)]
    rules = [_record_costs("f01", "rules", i, tools=1, models=0, approvals=0) for i in (1, 2, 3)]
    for item in agent:
        item["facts"]["cli"]["elapsed_seconds"] = 1.0
    for item in rules:
        item["facts"]["cli"]["elapsed_seconds"] = 60.0
    verdict_bits = "".join(compare(summarize(agent + rules)["rows"]))
    assert "不构成收益结论" in verdict_bits
    assert "工具调用" in verdict_bits


def test_render_markdown_shows_cost_columns():
    rows = summarize([_record_costs("f01", "rules", i) for i in (1, 2, 3)])
    text = render_markdown({"date": "2026-11-18", "arms": ["rules"]}, rows)
    assert "工具调用" in text and "模型调用" in text and "人工审批" in text


def test_gain_with_a_missing_dimension_is_not_claimed():
    """已知维度更强但有维度缺失：只说已知，不宣称整体收益。"""
    agent = [_record_costs("f01", "agent", i, correct=True) for i in (1, 2, 3)]
    for item in agent:
        item["facts"]["cli"]["elapsed_seconds"] = None
    rules = [_record_costs("f01", "rules", i, correct=i < 3) for i in (1, 2, 3)]
    for item in rules:
        item["facts"]["cli"]["elapsed_seconds"] = 60.0
    note = "".join(compare(summarize(agent + rules)["rows"]))
    assert "整体不构成收益结论（有维度缺失）" in note


def _spec(tmp_path):
    from pathlib import Path

    from aiops.evaluation.scenarios import ScenarioSpec

    script = Path(tmp_path) / "repeat.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    return ScenarioSpec(
        id="demo",
        name="演示",
        namespace="vigil-shop",
        service="demo",
        symptoms="",
        script=script,
        truth="演示",
        preflight_creates=True,
    )


def test_preflight_failure_without_creation_does_not_clean(monkeypatch, tmp_path):
    """预检失败、且脚本说没建东西时，绝不能去删既有对象。"""
    from aiops.evaluation import harness as H
    from aiops.evaluation.scenarios import VerbOutcome

    verbs: list[str] = []

    def fake_run_verb(spec, verb, *, settings):
        verbs.append(verb)
        facts = {"created": "0"} if verb == "preflight" else {}
        return VerbOutcome(verb=verb, ok=verb != "preflight", facts=facts, returncode=0, output="")

    async def fake_close(store, spec, record, settings, *, cleanup_needed):
        if cleanup_needed:
            record.environment_restored = False

    monkeypatch.setattr(H, "run_verb", fake_run_verb)
    monkeypatch.setattr(H, "_close", fake_close)
    record = asyncio.run(
        H.execute(None, _spec(tmp_path), "rules", 1, settings=object())  # type: ignore[arg-type]
    )
    assert "cleanup" not in verbs
    assert record.invalid_reason == "preflight"


def test_preflight_failure_after_creating_still_cleans(monkeypatch, tmp_path):
    from aiops.evaluation import harness as H
    from aiops.evaluation.scenarios import VerbOutcome

    cleaned: list[bool] = []

    def fake_run_verb(spec, verb, *, settings):
        facts = {"created": "1"} if verb == "preflight" else {}
        return VerbOutcome(verb=verb, ok=False, facts=facts, returncode=0, output="")

    async def fake_close(store, spec, record, settings, *, cleanup_needed):
        cleaned.append(cleanup_needed)

    monkeypatch.setattr(H, "run_verb", fake_run_verb)
    monkeypatch.setattr(H, "_close", fake_close)
    asyncio.run(
        H.execute(None, _spec(tmp_path), "rules", 1, settings=object())  # type: ignore[arg-type]
    )
    assert cleaned == [True]


def test_collection_failure_keeps_a_valid_arm_sample(monkeypatch, tmp_path):
    """故障已成立、臂已跑过：收尾采集失败不能把这条失败样本从分母里删掉。"""
    from aiops.evaluation import harness as H
    from aiops.evaluation.scenarios import VerbOutcome

    def fake_run_verb(spec, verb, *, settings):
        if verb == "recovered":
            raise OSError("采集失败")
        return VerbOutcome(verb=verb, ok=True, facts={}, returncode=0, output="")

    async def fake_close(store, spec, record, settings, *, cleanup_needed):
        return None

    monkeypatch.setattr(H, "run_verb", fake_run_verb)
    monkeypatch.setattr(H, "_close", fake_close)
    monkeypatch.setattr(H, "_invoke_arm", lambda spec, arm: (None, 5, "臂失败"))
    monkeypatch.setattr(H, "_dump_output", lambda *a, **kw: None)
    record = asyncio.run(
        H.execute(None, _spec(tmp_path), "rules", 1, settings=object())  # type: ignore[arg-type]
    )
    assert record.valid is True
    assert record.facts.get("collection_error")
