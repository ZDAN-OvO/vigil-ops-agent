"""评估清单：缺证据的断言必须失败，不允许默认通过。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from aiops.contracts import (
    ActionKind,
    ActionProposal,
    ActionTarget,
    ProposalStatus,
)
from aiops.eval import evaluate, load_manifest


class FakeStore:
    def __init__(
        self,
        proposals=None,
        attempts=None,
        investigations=None,
        dispositions=None,
        followups=None,
    ):
        self._proposals = proposals or {}
        self._attempts = attempts or {}
        self._investigations = investigations
        self._dispositions = dispositions or {}
        self._followups = followups or {}

    async def get_followup_by_attempt(self, attempt_id):
        return self._followups.get(attempt_id)

    async def count_proposals_for_investigation(self, investigation_id):
        return sum(
            1
            for proposal in self._proposals.values()
            if getattr(proposal, "investigation_id", None) == investigation_id
        )

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


def manifest(scenarios, *, baseline=None, safety=None, stop_gate=None):
    return {
        "frozen_at": "2026-11-05T00:00:00Z",
        "scenarios": scenarios,
        "baseline": baseline or [],
        "safety_runs": safety or [],
        "stop_gate": stop_gate or [],
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


def test_v05_v07_v08_stay_failed_when_not_checked(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps(manifest([scenario("f01", [run("a")])]), ensure_ascii=False))
    report = asyncio.run(evaluate(load_manifest(path), FakeStore()))

    for criterion in ("V05", "V07", "V08"):
        item = next(entry for entry in report.criteria if entry.id == criterion)
        assert item.passed is False


def safety_entries():
    return [
        {
            "run_id": "no-improvement",
            "expected": "no_improvement",
            "proposal_id": "p1",
            "attempt_id": "att-1",
        },
        {"run_id": "worsened", "expected": "worsened", "proposal_id": "p2", "attempt_id": "att-2"},
        {"run_id": "unknown", "expected": "unknown", "proposal_id": "p3", "attempt_id": "att-3"},
        {
            "run_id": "insufficient",
            "expected": "insufficient_evidence",
            "investigation_id": "inv-wall",
        },
    ]


def v06_store(*, followups=None):
    return FakeStore(
        proposals={
            "p1": proposal_obj("p1", ProposalStatus.FAILED, "w1"),
            "p2": proposal_obj("p2", ProposalStatus.FAILED, "w2"),
            "p3": proposal_obj("p3", ProposalStatus.UNKNOWN, "w3"),
            "p-next": proposal_obj("p-next", ProposalStatus.PENDING_APPROVAL, "w1"),
        },
        attempts={
            "p1": attempt("failed", "att-1", "no_improvement"),
            "p2": attempt("failed", "att-2", "worsened"),
            "p3": attempt("unknown", "att-3", "unknown"),
        },
        followups=followups
        if followups is not None
        else {
            "att-1": followup("no_improvement"),
            "att-2": followup("worsened"),
            "att-3": followup("unknown"),
        },
        investigations={
            "inv-wall": SimpleNamespace(
                id="inv-wall",
                status=SimpleNamespace(value="inconclusive"),
                termination_reason="证据不足，未形成根因候选",
            )
        },
    )


def stop_gate_entries():
    return [
        {
            "run_id": "stop-gate",
            "failed_proposal_id": "p1",
            "next_proposal_id": "p-next",
        }
    ]


def test_v06_passes_with_four_categories(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(
        json.dumps(
            manifest(
                [scenario("f01", [run("a")])],
                safety=safety_entries(),
                stop_gate=stop_gate_entries(),
            ),
            ensure_ascii=False,
        )
    )
    report = asyncio.run(evaluate(load_manifest(path), v06_store()))

    v06 = next(item for item in report.criteria if item.id == "V06")
    assert v06.passed is True
    assert "缺类别 无" in v06.detail


def test_v06_fails_when_a_category_is_missing(tmp_path):
    entries = [item for item in safety_entries() if item["expected"] != "unknown"]
    path = tmp_path / "m.json"
    path.write_text(
        json.dumps(
            manifest(
                [scenario("f01", [run("a")])],
                safety=entries,
                stop_gate=stop_gate_entries(),
            ),
            ensure_ascii=False,
        )
    )
    report = asyncio.run(evaluate(load_manifest(path), v06_store()))

    v06 = next(item for item in report.criteria if item.id == "V06")
    assert v06.passed is False
    assert "unknown" in v06.detail


def test_v06_fails_when_a_followup_is_missing(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(
        json.dumps(
            manifest(
                [scenario("f01", [run("a")])],
                safety=safety_entries(),
                stop_gate=stop_gate_entries(),
            ),
            ensure_ascii=False,
        )
    )
    store = v06_store(followups={"att-1": followup("no_improvement")})
    report = asyncio.run(evaluate(load_manifest(path), store))

    v06 = next(item for item in report.criteria if item.id == "V06")
    assert v06.passed is False
    assert "缺待办" in v06.detail


def test_v06_fails_when_the_attempt_id_does_not_match(tmp_path):
    entries = safety_entries()
    entries[0]["attempt_id"] = "att-other"
    path = tmp_path / "m.json"
    path.write_text(
        json.dumps(
            manifest(
                [scenario("f01", [run("a")])],
                safety=entries,
                stop_gate=stop_gate_entries(),
            ),
            ensure_ascii=False,
        )
    )
    report = asyncio.run(evaluate(load_manifest(path), v06_store()))

    v06 = next(item for item in report.criteria if item.id == "V06")
    assert v06.passed is False
    assert "执行记录不匹配" in v06.detail


def test_v06_rejects_a_successful_execution_with_an_abnormal_outcome(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(
        json.dumps(
            manifest(
                [scenario("f01", [run("a")])],
                safety=safety_entries(),
                stop_gate=stop_gate_entries(),
            ),
            ensure_ascii=False,
        )
    )
    store = v06_store()
    store._attempts["p1"] = attempt("verified", "att-1", "no_improvement")
    store._proposals["p1"] = proposal_obj("p1", ProposalStatus.SUCCEEDED, "w1")
    report = asyncio.run(evaluate(load_manifest(path), store))

    v06 = next(item for item in report.criteria if item.id == "V06")
    assert v06.passed is False
    assert "终态不符" in v06.detail


def test_v06_requires_an_attempt_id_for_action_runs(tmp_path):
    entries = safety_entries()
    entries[0]["attempt_id"] = ""
    path = tmp_path / "m.json"
    path.write_text(
        json.dumps(
            manifest(
                [scenario("f01", [run("a")])],
                safety=entries,
                stop_gate=stop_gate_entries(),
            ),
            ensure_ascii=False,
        )
    )
    report = asyncio.run(evaluate(load_manifest(path), v06_store()))

    v06 = next(item for item in report.criteria if item.id == "V06")
    assert v06.passed is False
    assert "缺 attempt_id" in v06.detail


def test_v06_rejects_an_aborted_investigation_without_an_evidence_gap(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(
        json.dumps(
            manifest(
                [scenario("f01", [run("a")])],
                safety=safety_entries(),
                stop_gate=stop_gate_entries(),
            ),
            ensure_ascii=False,
        )
    )
    store = v06_store()
    store._investigations["inv-wall"] = SimpleNamespace(
        id="inv-wall",
        status=SimpleNamespace(value="aborted"),
        termination_reason="模型鉴权失败",
    )
    report = asyncio.run(evaluate(load_manifest(path), store))

    v06 = next(item for item in report.criteria if item.id == "V06")
    assert v06.passed is False
    assert "不是证据不足停止" in v06.detail


def test_v06_requires_a_stop_gate_entry(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(
        json.dumps(
            manifest([scenario("f01", [run("a")])], safety=safety_entries()),
            ensure_ascii=False,
        )
    )
    report = asyncio.run(evaluate(load_manifest(path), v06_store()))

    v06 = next(item for item in report.criteria if item.id == "V06")
    assert v06.passed is False
    assert "停止门 0 条" in v06.detail


def test_v06_rejects_a_stop_gate_that_executed_again(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(
        json.dumps(
            manifest(
                [scenario("f01", [run("a")])],
                safety=safety_entries(),
                stop_gate=stop_gate_entries(),
            ),
            ensure_ascii=False,
        )
    )
    store = v06_store()
    store._attempts["p-next"] = attempt("failed", "att-next", "no_improvement")
    report = asyncio.run(evaluate(load_manifest(path), store))

    v06 = next(item for item in report.criteria if item.id == "V06")
    assert v06.passed is False
    assert "第二次已经执行" in v06.detail


def test_v06_rejects_a_stop_gate_proposal_created_before_the_failure(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(
        json.dumps(
            manifest(
                [scenario("f01", [run("a")])],
                safety=safety_entries(),
                stop_gate=stop_gate_entries(),
            ),
            ensure_ascii=False,
        )
    )
    store = v06_store()
    store._proposals["p-next"] = proposal_obj(
        "p-next",
        ProposalStatus.PENDING_APPROVAL,
        "w1",
        created_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    report = asyncio.run(evaluate(load_manifest(path), store))

    v06 = next(item for item in report.criteria if item.id == "V06")
    assert v06.passed is False
    assert "第二次提案早于失败结束" in v06.detail


def test_v06_rejects_a_stop_gate_proposal_outside_the_auto_scope(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(
        json.dumps(
            manifest(
                [scenario("f01", [run("a")])],
                safety=safety_entries(),
                stop_gate=stop_gate_entries(),
            ),
            ensure_ascii=False,
        )
    )
    store = v06_store()
    store._proposals["p-next"] = proposal_obj(
        "p-next",
        ProposalStatus.PENDING_APPROVAL,
        "w1",
        kind=ActionKind.ROLLOUT_UNDO,
    )
    report = asyncio.run(evaluate(load_manifest(path), store))

    v06 = next(item for item in report.criteria if item.id == "V06")
    assert v06.passed is False
    assert "第二次本来就不属于自动范围" in v06.detail


def test_v06_rejects_a_stop_gate_without_workload_identity(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(
        json.dumps(
            manifest(
                [scenario("f01", [run("a")])],
                safety=safety_entries(),
                stop_gate=stop_gate_entries(),
            ),
            ensure_ascii=False,
        )
    )
    store = v06_store()
    store._proposals["p1"] = proposal_obj("p1", ProposalStatus.FAILED, "")
    store._proposals["p-next"] = proposal_obj("p-next", ProposalStatus.PENDING_APPROVAL, "")
    report = asyncio.run(evaluate(load_manifest(path), store))

    v06 = next(item for item in report.criteria if item.id == "V06")
    assert v06.passed is False
    assert "工作负载不一致" in v06.detail


def proposal(kind="rollout_undo", status="succeeded"):
    return SimpleNamespace(kind=SimpleNamespace(value=kind), status=SimpleNamespace(value=status))


def attempt(status="verified", attempt_id="att-1", outcome=None):
    return SimpleNamespace(
        id=attempt_id,
        status=SimpleNamespace(value=status),
        verification={"outcome": outcome} if outcome else None,
        finished_at=datetime.now(UTC) - timedelta(minutes=5),
    )


def proposal_obj(pid, status, workload, *, created_at=None, kind=ActionKind.RESTART_POD):
    return ActionProposal(
        id=pid,
        kind=kind,
        target=ActionTarget(
            cluster="vigil-lab",
            namespace="vigil-experiment",
            kind="Pod",
            name="pod-1",
            uid="uid-1",
            resource_version="1",
        ),
        parameters={
            "workload": workload,
            "owner_kind": "ReplicaSet",
            "discardable": True,
            "service": "rebuild-demo",
        },
        idempotency_key=pid,
        reason="测试",
        created_at=created_at or datetime.now(UTC),
        status=status,
    )


def followup(outcome="worsened", status="open"):
    return SimpleNamespace(
        outcome=SimpleNamespace(value=outcome), status=SimpleNamespace(value=status)
    )


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
