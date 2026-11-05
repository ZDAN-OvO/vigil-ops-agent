"""评估清单：缺证据的断言必须失败，不允许默认通过。"""

import asyncio
import json
from types import SimpleNamespace

from aiops.eval import evaluate, load_manifest


class FakeStore:
    def __init__(self, proposals=None, attempts=None, investigations=None, dispositions=None):
        self._proposals = proposals or {}
        self._attempts = attempts or {}
        self._investigations = investigations
        self._dispositions = dispositions or {}

    async def get_proposal_disposition(self, proposal_id):
        return self._dispositions.get(proposal_id, "approval")

    async def get_investigation(self, investigation_id):
        if self._investigations is not None:
            return self._investigations.get(investigation_id)
        return SimpleNamespace(id=investigation_id, status=SimpleNamespace(value="completed"))

    async def get_proposal(self, proposal_id):
        return self._proposals.get(proposal_id)

    async def get_attempt(self, proposal_id):
        return self._attempts.get(proposal_id)


def scenario(scenario_id, runs, *, core=True, signals=None):
    return {
        "id": scenario_id,
        "version": 1,
        "card": "docs/mvp.md",
        "signal_classes": signals or ["platform", "business"],
        "allowed_actions": [],
        "core": core,
        "runs": runs,
    }


def run(
    run_id,
    *,
    valid=True,
    correct=False,
    proposal="",
    attempt="",
    recovered=False,
    investigation=None,
):
    return {
        "run_id": run_id,
        "valid": valid,
        "reason": "记录",
        "investigation_id": investigation if investigation is not None else f"inv-{run_id}",
        "proposal_id": proposal,
        "attempt_id": attempt,
        "recovered": recovered,
        "investigation_correct": correct,
    }


def manifest(scenarios, *, baseline=None):
    return {
        "frozen_at": "2026-11-05T00:00:00Z",
        "scenarios": scenarios,
        "baseline": baseline or [],
        "demo_packs": [],
    }


def test_thin_runs_fail_v02(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(
        json.dumps(manifest([scenario("f01", [run("a", correct=True)])]), ensure_ascii=False)
    )
    report = asyncio.run(evaluate(load_manifest(path), FakeStore()))

    v02 = next(item for item in report.criteria if item.id == "V02")
    assert v02.passed is False
    assert "f01=1" in v02.detail


def test_three_valid_runs_with_one_correct_pass_v02(tmp_path):
    path = tmp_path / "m.json"
    payload = manifest([scenario("f01", [run("a", correct=True), run("b"), run("c")])])
    path.write_text(json.dumps(payload, ensure_ascii=False))
    report = asyncio.run(evaluate(load_manifest(path), FakeStore()))

    assert next(item for item in report.criteria if item.id == "V02").passed is True


def test_non_core_scenario_is_not_counted_in_v02(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(
        json.dumps(
            manifest(
                [
                    scenario("f01", [run("a", correct=True), run("b"), run("c")]),
                    scenario("a01", [run("x")], core=False),
                ]
            ),
            ensure_ascii=False,
        )
    )
    report = asyncio.run(evaluate(load_manifest(path), FakeStore()))

    assert next(item for item in report.criteria if item.id == "V02").passed is True


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


def test_v04_passes_only_with_all_three_registered_paths(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps(manifest([scenario("f01", v04_runs())]), ensure_ascii=False))

    report = asyncio.run(evaluate(load_manifest(path), v04_store()))

    v04 = next(item for item in report.criteria if item.id == "V04")
    assert v04.passed is True
    assert "自动允许并验证 1 条" in v04.detail
    assert "审批后执行 1 次" in v04.detail


def test_missing_auto_path_fails_v04(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps(manifest([scenario("f01", v04_runs())]), ensure_ascii=False))
    store = v04_store(dispositions={"act-manual": "approval"})

    report = asyncio.run(evaluate(load_manifest(path), store))

    v04 = next(item for item in report.criteria if item.id == "V04")
    assert v04.passed is False
    assert "自动允许并验证 0 条" in v04.detail


def test_auto_evidence_outside_the_manifest_does_not_count(tmp_path):
    """库里另有自动成功记录，但本次清单没有登记，不能算 V04 通过。"""
    path = tmp_path / "m.json"
    runs = [
        run("manual", proposal="act-manual", attempt="att-manual"),
        run("denied", proposal="act-denied"),
    ]
    path.write_text(json.dumps(manifest([scenario("f01", runs)]), ensure_ascii=False))
    store = v04_store(
        proposals={
            "act-manual": proposal("rollout_undo"),
            "act-denied": proposal("restart_pod", status="denied"),
            "act-hidden": proposal("restart_pod"),
        },
        attempts={"act-manual": attempt("verified"), "act-hidden": attempt("verified")},
        dispositions={"act-manual": "approval", "act-hidden": "auto"},
    )

    report = asyncio.run(evaluate(load_manifest(path), store))

    v04 = next(item for item in report.criteria if item.id == "V04")
    assert v04.passed is False
    assert "自动允许并验证 0 条" in v04.detail


def test_v05_to_v08_stay_failed_when_not_checked(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps(manifest([scenario("f01", [run("a")])]), ensure_ascii=False))
    report = asyncio.run(evaluate(load_manifest(path), FakeStore()))

    for criterion in ("V05", "V06", "V07", "V08"):
        item = next(entry for entry in report.criteria if entry.id == criterion)
        assert item.passed is False


def proposal(kind="rollout_undo", status="succeeded"):
    return SimpleNamespace(kind=SimpleNamespace(value=kind), status=SimpleNamespace(value=status))


def attempt(status="verified", attempt_id="att-1"):
    return SimpleNamespace(id=attempt_id, status=SimpleNamespace(value=status))


def test_duplicate_run_ids_and_missing_investigations_do_not_count(tmp_path):
    path = tmp_path / "m.json"
    runs = [run("a", correct=True), run("a"), run("b")]
    path.write_text(json.dumps(manifest([scenario("f01", runs)]), ensure_ascii=False))
    store = FakeStore(
        investigations={"inv-a": SimpleNamespace(id="inv-a"), "inv-b": SimpleNamespace(id="inv-b")}
    )

    report = asyncio.run(evaluate(load_manifest(path), store))

    v02 = next(item for item in report.criteria if item.id == "V02")
    assert v02.passed is False
    assert "重复" in v02.detail


def test_missing_investigation_is_not_a_valid_run(tmp_path):
    path = tmp_path / "m.json"
    runs = [run("a", correct=True), run("b"), run("c")]
    path.write_text(json.dumps(manifest([scenario("f01", runs)]), ensure_ascii=False))
    store = FakeStore(investigations={})

    report = asyncio.run(evaluate(load_manifest(path), store))

    v02 = next(item for item in report.criteria if item.id == "V02")
    assert v02.passed is False
    assert "调查不存在" in v02.detail


def test_action_recovery_requires_a_verified_attempt(tmp_path):
    path = tmp_path / "m.json"
    runs = [
        run("r1", proposal="act-1", attempt="att-1", recovered=True),
        run("r2", proposal="act-2", attempt="att-2", recovered=True),
    ]
    payload = manifest(
        [scenario("f01", runs, signals=["platform", "business"])],
    )
    payload["scenarios"][0]["allowed_actions"] = ["rollout_undo", "restart_pod"]
    path.write_text(json.dumps(payload, ensure_ascii=False))
    store = FakeStore(
        proposals={"act-1": proposal("rollout_undo"), "act-2": proposal("restart_pod")},
        attempts={"act-1": attempt("verified"), "act-2": attempt("failed")},
    )

    report = asyncio.run(evaluate(load_manifest(path), store))

    v03 = next(item for item in report.criteria if item.id == "V03")
    assert v03.passed is False
    assert "r2" in v03.detail


def test_auto_disposition_without_a_verified_attempt_fails_v04(tmp_path):
    """策略放行但执行没验证，不能算自动路径可验证。"""
    path = tmp_path / "m.json"
    path.write_text(json.dumps(manifest([scenario("f01", v04_runs())]), ensure_ascii=False))
    store = v04_store(attempts={"act-auto": attempt("failed"), "act-manual": attempt("verified")})

    report = asyncio.run(evaluate(load_manifest(path), store))

    v04 = next(item for item in report.criteria if item.id == "V04")
    assert v04.passed is False
    assert "自动允许并验证 0 条" in v04.detail


def test_v04_needs_registered_denied_evidence(tmp_path):
    """没有登记过拒绝提案时，空口声称“被拒绝提案未执行”不算证据。"""
    path = tmp_path / "m.json"
    runs = [
        run("auto", proposal="act-auto", attempt="att-auto"),
        run("manual", proposal="act-manual", attempt="att-manual"),
    ]
    path.write_text(json.dumps(manifest([scenario("f01", runs)]), ensure_ascii=False))

    report = asyncio.run(evaluate(load_manifest(path), v04_store()))

    v04 = next(item for item in report.criteria if item.id == "V04")
    assert v04.passed is False
    assert "拒绝证据 无" in v04.detail


def test_empty_manifest_cannot_pass(tmp_path):
    path = tmp_path / "m.json"
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
    path.write_text(json.dumps(payload, ensure_ascii=False))

    report = asyncio.run(evaluate(load_manifest(path), FakeStore()))

    passed = {item.id: item.passed for item in report.criteria}
    assert passed["V01"] is False
    assert passed["V02"] is False
    assert passed["V09"] is False
    assert passed["V10"] is False


def test_baseline_needs_a_real_investigation(tmp_path):
    path = tmp_path / "m.json"
    payload = manifest([scenario("f01", [run("a", correct=True), run("b"), run("c")])])
    payload["baseline"] = [{"scenario": "f01", "run_id": "b1", "rules_only": True}]
    path.write_text(json.dumps(payload, ensure_ascii=False))
    store = FakeStore(investigations={})

    report = asyncio.run(evaluate(load_manifest(path), store))

    assert next(item for item in report.criteria if item.id == "V09").passed is False


def test_verified_attempt_without_recovered_flag_is_not_a_recovery(tmp_path):
    """另一动作确实恢复了，缺 recovered 标记的那个不能被算进去。"""
    path = tmp_path / "m.json"
    runs = [
        run("r1", proposal="act-1", attempt="att-1", recovered=True),
        run("r2", proposal="act-2", attempt="att-2", recovered=False),
    ]
    payload = manifest([scenario("f01", runs)])
    payload["scenarios"][0]["allowed_actions"] = ["rollout_undo", "restart_pod"]
    path.write_text(json.dumps(payload, ensure_ascii=False))
    store = FakeStore(
        proposals={"act-1": proposal("rollout_undo"), "act-2": proposal("restart_pod")},
        attempts={"act-1": attempt("verified"), "act-2": attempt("verified")},
    )

    report = asyncio.run(evaluate(load_manifest(path), store))

    v03 = next(item for item in report.criteria if item.id == "V03")
    assert v03.passed is False
    assert "restart_pod" not in v03.detail


def test_invalid_denied_run_with_an_attempt_still_fails_v04(tmp_path):
    """被拒绝提案不得执行是安全不变量，不因运行被标为无效而跳过。"""
    path = tmp_path / "m.json"
    runs = v04_runs()
    runs[2]["valid"] = False
    path.write_text(json.dumps(manifest([scenario("f01", runs)]), ensure_ascii=False))
    store = v04_store(
        attempts={
            "act-auto": attempt("verified", "att-auto"),
            "act-manual": attempt("verified", "att-manual"),
            "act-denied": attempt("verified", "att-denied"),
        }
    )

    report = asyncio.run(evaluate(load_manifest(path), store))

    v04 = next(item for item in report.criteria if item.id == "V04")
    assert v04.passed is False
    assert "被拒绝提案未执行 False" in v04.detail


def test_v04_requires_the_registered_attempt_id(tmp_path):
    path = tmp_path / "m.json"
    runs = v04_runs()
    runs[0]["attempt_id"] = "att-other"
    path.write_text(json.dumps(manifest([scenario("f01", runs)]), ensure_ascii=False))

    report = asyncio.run(evaluate(load_manifest(path), v04_store()))

    v04 = next(item for item in report.criteria if item.id == "V04")
    assert v04.passed is False
    assert "自动允许并验证 0 条" in v04.detail


def test_denied_proposal_with_an_attempt_fails_v04(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps(manifest([scenario("f01", v04_runs())]), ensure_ascii=False))
    store = v04_store(
        attempts={
            "act-auto": attempt("verified"),
            "act-manual": attempt("verified"),
            "act-denied": attempt("verified"),
        }
    )

    report = asyncio.run(evaluate(load_manifest(path), store))

    v04 = next(item for item in report.criteria if item.id == "V04")
    assert v04.passed is False
    assert "被拒绝提案未执行 False" in v04.detail
