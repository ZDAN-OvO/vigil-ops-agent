"""评估编排的纯函数部分：汇总、门槛与分批合并。"""

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


def test_compare_reports_leaders_when_two_arms_qualify():
    records = [_record("f01", "rules", i) for i in (1, 2, 3)]
    records += [_record("f01", "agent", i) for i in (1, 2, 3)]
    notes = compare(summarize(records)["rows"])
    assert "agent/rules" in notes[0] or "rules/agent" in notes[0]


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
