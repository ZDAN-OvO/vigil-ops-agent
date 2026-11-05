"""评估清单：缺证据的断言必须失败，不允许默认通过。"""

import asyncio
import json
from types import SimpleNamespace

from aiops.eval import evaluate, load_manifest


class FakeStore:
    def __init__(self, proposals=None, attempts=None, investigations=None, auto_count=0):
        self._proposals = proposals or {}
        self._attempts = attempts or {}
        self._investigations = investigations
        self._auto_count = auto_count

    async def count_proposals_by_disposition(self, disposition):
        return self._auto_count if disposition == "auto" else 0

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


def test_missing_auto_path_fails_v04(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(
        json.dumps(
            manifest([scenario("f01", [run("a", proposal="act-1", attempt="att-1")])]),
            ensure_ascii=False,
        )
    )
    proposals = {
        "act-1": SimpleNamespace(
            kind=SimpleNamespace(value="rollout_undo"), status=SimpleNamespace(value="succeeded")
        )
    }
    report = asyncio.run(evaluate(load_manifest(path), FakeStore(proposals)))

    v04 = next(item for item in report.criteria if item.id == "V04")
    assert v04.passed is False
    assert "未提供" in v04.detail


def test_v05_to_v08_stay_failed_when_not_checked(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps(manifest([scenario("f01", [run("a")])]), ensure_ascii=False))
    report = asyncio.run(evaluate(load_manifest(path), FakeStore()))

    for criterion in ("V05", "V06", "V07", "V08"):
        item = next(entry for entry in report.criteria if entry.id == criterion)
        assert item.passed is False


def proposal(kind="rollout_undo", status="succeeded"):
    return SimpleNamespace(kind=SimpleNamespace(value=kind), status=SimpleNamespace(value=status))


def attempt(status="verified"):
    return SimpleNamespace(status=SimpleNamespace(value=status))


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


def test_v04_needs_an_auto_disposition_in_the_database(tmp_path):
    path = tmp_path / "m.json"
    runs = [run("r1", proposal="act-1", attempt="att-1")]
    path.write_text(json.dumps(manifest([scenario("f01", runs)]), ensure_ascii=False))
    store = FakeStore(
        proposals={"act-1": proposal("rollout_undo")},
        attempts={"act-1": attempt("verified")},
        auto_count=0,
    )

    report = asyncio.run(evaluate(load_manifest(path), store))

    v04 = next(item for item in report.criteria if item.id == "V04")
    assert v04.passed is False
    assert "未提供" in v04.detail

    store._auto_count = 1
    report = asyncio.run(evaluate(load_manifest(path), store))
    assert next(item for item in report.criteria if item.id == "V04").passed is True


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
    path = tmp_path / "m.json"
    runs = [run("r1", proposal="act-1", attempt="att-1", recovered=False)]
    payload = manifest([scenario("f01", runs)])
    payload["scenarios"][0]["allowed_actions"] = ["rollout_undo"]
    path.write_text(json.dumps(payload, ensure_ascii=False))
    store = FakeStore(
        proposals={"act-1": proposal("rollout_undo")},
        attempts={"act-1": attempt("verified")},
    )

    report = asyncio.run(evaluate(load_manifest(path), store))

    assert next(item for item in report.criteria if item.id == "V03").passed is False


def test_denied_proposal_with_an_attempt_fails_v04(tmp_path):
    path = tmp_path / "m.json"
    runs = [run("r1", proposal="act-1", attempt="att-1")]
    path.write_text(json.dumps(manifest([scenario("f01", runs)]), ensure_ascii=False))
    store = FakeStore(
        proposals={"act-1": proposal("rollout_undo", status="denied")},
        attempts={"act-1": attempt("verified")},
        auto_count=1,
    )

    report = asyncio.run(evaluate(load_manifest(path), store))

    assert next(item for item in report.criteria if item.id == "V04").passed is False
