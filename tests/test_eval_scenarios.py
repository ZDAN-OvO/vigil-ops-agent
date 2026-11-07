"""评估清单：场景、恢复、自动允许与空清单都不能默认通过。"""

from types import SimpleNamespace

from eval_fakes import (
    FakeStore,
    attempt,
    check,
    criterion,
    manifest,
    proposal,
    run,
    scenario,
)


def v04_runs():
    """V04 三条路径各一条登记运行：自动、人工审批、拒绝。"""
    return [
        run("auto", proposal="act-auto", attempt="att-auto"),
        run("manual", proposal="act-manual", attempt="att-manual"),
        run("denied", proposal="act-denied"),
    ]


def v04_store(**overrides):
    values = {
        "proposals": {
            "act-auto": proposal("restart_pod"),
            "act-manual": proposal("rollout_undo"),
            "act-denied": proposal("restart_pod", status="denied"),
        },
        "attempts": {
            "act-auto": attempt("verified", "att-auto"),
            "act-manual": attempt("verified", "att-manual"),
        },
        "dispositions": {"act-auto": "auto", "act-manual": "approval"},
    }
    values.update(overrides)
    return FakeStore(**values)


def test_thin_runs_fail_v02(tmp_path):
    report = check(tmp_path, manifest([scenario("f01", [run("a", correct=True)])]), FakeStore())

    v02 = criterion(report, "V02")
    assert v02.passed is False
    assert "f01=1" in v02.detail


def test_three_valid_runs_with_one_correct_pass_v02(tmp_path):
    payload = manifest([scenario("f01", [run("a", correct=True), run("b"), run("c")])])

    assert criterion(check(tmp_path, payload, FakeStore()), "V02").passed is True


def test_non_core_scenario_is_not_counted_in_v02(tmp_path):
    payload = manifest(
        [
            scenario("f01", [run("a", correct=True), run("b"), run("c")]),
            scenario("a01", [run("x")], core=False),
        ]
    )

    assert criterion(check(tmp_path, payload, FakeStore()), "V02").passed is True


def test_duplicate_run_ids_and_missing_investigations_do_not_count(tmp_path):
    runs = [run("a", correct=True), run("a"), run("b")]
    store = FakeStore(
        investigations={"inv-a": SimpleNamespace(id="inv-a"), "inv-b": SimpleNamespace(id="inv-b")}
    )

    v02 = criterion(check(tmp_path, manifest([scenario("f01", runs)]), store), "V02")
    assert v02.passed is False
    assert "重复" in v02.detail


def test_missing_investigation_is_not_a_valid_run(tmp_path):
    runs = [run("a", correct=True), run("b"), run("c")]

    v02 = criterion(
        check(tmp_path, manifest([scenario("f01", runs)]), FakeStore(investigations={})), "V02"
    )
    assert v02.passed is False
    assert "调查不存在" in v02.detail


def test_action_recovery_requires_a_verified_attempt(tmp_path):
    runs = [
        run("r1", proposal="act-1", attempt="att-1", recovered=True),
        run("r2", proposal="act-2", attempt="att-2", recovered=True),
    ]
    payload = manifest([scenario("f01", runs, signals=["platform", "business"])])
    payload["scenarios"][0]["allowed_actions"] = ["rollout_undo", "restart_pod"]
    store = FakeStore(
        proposals={"act-1": proposal("rollout_undo"), "act-2": proposal("restart_pod")},
        attempts={"act-1": attempt("verified"), "act-2": attempt("failed")},
    )

    v03 = criterion(check(tmp_path, payload, store), "V03")
    assert v03.passed is False
    assert "r2" in v03.detail


def test_verified_attempt_without_recovered_flag_is_not_a_recovery(tmp_path):
    runs = [
        run("r1", proposal="act-1", attempt="att-1", recovered=True),
        run("r2", proposal="act-2", attempt="att-2", recovered=False),
    ]
    payload = manifest([scenario("f01", runs)])
    payload["scenarios"][0]["allowed_actions"] = ["rollout_undo", "restart_pod"]
    store = FakeStore(
        proposals={"act-1": proposal("rollout_undo"), "act-2": proposal("restart_pod")},
        attempts={"act-1": attempt("verified"), "act-2": attempt("verified")},
    )

    v03 = criterion(check(tmp_path, payload, store), "V03")
    assert v03.passed is False
    assert "restart_pod" not in v03.detail


def test_v04_passes_only_with_all_three_registered_paths(tmp_path):
    report = check(tmp_path, manifest([scenario("f01", v04_runs())]), v04_store())

    v04 = criterion(report, "V04")
    assert v04.passed is True
    assert "自动允许并验证 1 条" in v04.detail
    assert "审批后执行 1 次" in v04.detail


def test_missing_auto_path_fails_v04(tmp_path):
    store = v04_store(dispositions={"act-manual": "approval"})

    v04 = criterion(check(tmp_path, manifest([scenario("f01", v04_runs())]), store), "V04")
    assert v04.passed is False
    assert "自动允许并验证 0 条" in v04.detail


def test_auto_evidence_outside_the_manifest_does_not_count(tmp_path):
    """库里另有自动成功记录，但本次清单没有登记，不能算 V04 通过。"""
    runs = [
        run("manual", proposal="act-manual", attempt="att-manual"),
        run("denied", proposal="act-denied"),
    ]
    store = v04_store(
        proposals={
            "act-manual": proposal("rollout_undo"),
            "act-denied": proposal("restart_pod", status="denied"),
            "act-hidden": proposal("restart_pod"),
        },
        attempts={
            "act-manual": attempt("verified", "att-manual"),
            "act-hidden": attempt("verified"),
        },
        dispositions={"act-manual": "approval", "act-hidden": "auto"},
    )

    v04 = criterion(check(tmp_path, manifest([scenario("f01", runs)]), store), "V04")
    assert v04.passed is False
    assert "自动允许并验证 0 条" in v04.detail


def test_auto_disposition_without_a_verified_attempt_fails_v04(tmp_path):
    """策略放行但执行没验证，不能算自动路径可验证。"""
    store = v04_store(attempts={"act-auto": attempt("failed"), "act-manual": attempt("verified")})

    v04 = criterion(check(tmp_path, manifest([scenario("f01", v04_runs())]), store), "V04")
    assert v04.passed is False
    assert "自动允许并验证 0 条" in v04.detail


def test_v04_needs_registered_denied_evidence(tmp_path):
    """没有登记过拒绝提案时，空口声称“被拒绝提案未执行”不算证据。"""
    runs = [
        run("auto", proposal="act-auto", attempt="att-auto"),
        run("manual", proposal="act-manual", attempt="att-manual"),
    ]

    v04 = criterion(check(tmp_path, manifest([scenario("f01", runs)]), v04_store()), "V04")
    assert v04.passed is False
    assert "拒绝证据 无" in v04.detail


def test_invalid_denied_run_with_an_attempt_still_fails_v04(tmp_path):
    """被拒绝提案不得执行是安全不变量，不因运行被标为无效而跳过。"""
    runs = v04_runs()
    runs[2]["valid"] = False
    store = v04_store(
        attempts={
            "act-auto": attempt("verified", "att-auto"),
            "act-manual": attempt("verified", "att-manual"),
            "act-denied": attempt("verified", "att-denied"),
        }
    )

    v04 = criterion(check(tmp_path, manifest([scenario("f01", runs)]), store), "V04")
    assert v04.passed is False
    assert "被拒绝提案未执行 False" in v04.detail


def test_v04_requires_the_registered_attempt_id(tmp_path):
    runs = v04_runs()
    runs[0]["attempt_id"] = "att-other"

    v04 = criterion(check(tmp_path, manifest([scenario("f01", runs)]), v04_store()), "V04")
    assert v04.passed is False
    assert "自动允许并验证 0 条" in v04.detail


def test_empty_manifest_cannot_pass(tmp_path):
    payload = {
        "frozen_at": "2026-11-05T00:00:00Z",
        "scenarios": [
            {
                "id": "fake",
                "version": 1,
                "card": "missing-card.md#missing",
                "signal_classes": ["platform", "business"],
                "allowed_actions": [],
                "core": False,
                "runs": [],
            }
        ],
        "baseline": [{"scenario": "missing", "run_id": "missing-baseline"}],
        "demo_packs": [],
    }

    report = check(tmp_path, payload, FakeStore())

    passed = {item.id: item.passed for item in report.criteria}
    assert passed["V01"] is False
    assert passed["V02"] is False
    assert passed["V09"] is False
    assert passed["V10"] is False


def test_baseline_needs_a_real_investigation(tmp_path):
    payload = manifest([scenario("f01", [run("a", correct=True), run("b"), run("c")])])
    payload["baseline"] = [
        {
            "scenario": "f01",
            "run_id": "b1",
            "rules_version": "rules-1",
            "agent_run_ids": ["a"],
            "compared": ["调查结论"],
            "limitations": [],
        }
    ]

    report = check(tmp_path, payload, FakeStore(investigations={}))

    assert criterion(report, "V09").passed is False


def test_v05_v07_v08_stay_failed_when_not_checked(tmp_path):
    report = check(tmp_path, manifest([scenario("f01", [run("a")])]), FakeStore())

    for criterion_id in ("V05", "V07", "V08"):
        assert criterion(report, criterion_id).passed is False
