"""V07：重复请求不重复写入，原始观测可回查。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from eval_fakes import (
    FakeStore,
    ProposalStatus,
    approval_for,
    attempt,
    audit,
    check,
    criterion,
    manifest,
    proposal_obj,
    run,
    scenario,
    write_evidence,
)


def v07_entries(evidence):
    return {
        "idempotency": [
            {
                "run_id": "concurrent",
                "kind": "approval",
                "proposal_id": "act-once",
                "expected_approvals": 1,
                "expected_attempts": 1,
                "evidence": evidence,
                "markers": ["ok"],
            },
            {
                "run_id": "duplicate-resume",
                "kind": "resume",
                "proposal_id": "act-once",
                "expected_approvals": 1,
                "expected_attempts": 1,
                "evidence": evidence,
                "markers": ["ok"],
            },
        ],
        "dedup": [
            {
                "run_id": "dedup",
                "incident_id": "inc-1",
                "investigation_id": "inv-dedup",
                "fingerprint": "fp-1",
                "window_minutes": 10,
                "evidence": evidence,
                "markers": ["ok"],
            }
        ],
        "trace": [
            {
                "run_id": "trace",
                "investigation_id": "inv-trace",
                "evidence": evidence,
                "markers": ["ok"],
            }
        ],
    }


def v07_store():
    accepted = proposal_obj("act-once", ProposalStatus.SUCCEEDED, "w1")
    incident = SimpleNamespace(
        alert_ids=["fp-1"], time_range=SimpleNamespace(start=datetime.now(UTC))
    )
    stamp = datetime.now(UTC) - timedelta(days=1)
    return FakeStore(
        proposals={"act-once": accepted},
        attempts={"act-once": attempt("verified", "att-once")},
        approvals={"act-once": approval_for(accepted)},
        audits={
            "act-once": [audit("action.approved", payload={"approval_id": "apr-act-once"})],
            "inv-dedup": [audit("alert.received", payload={"fingerprints": ["fp-1"]})],
        },
        incidents={"inc-1": incident},
        incident_created={"inc-1": stamp},
        receipts={"fp-1": [stamp]},
        evidence={
            "inv-trace": [
                SimpleNamespace(
                    id="ev-1",
                    source="kubernetes",
                    relation="supporting",
                    observation="pods listed",
                    raw_ref="evidence/inv-trace/1.json",
                    tool_call_id="tc-1",
                )
            ]
        },
        tool_calls={
            "inv-trace": [
                SimpleNamespace(
                    id="tc-1",
                    started_at=stamp,
                    finished_at=stamp + timedelta(seconds=1),
                    error_code=None,
                    raw_ref="evidence/inv-trace/1.json",
                )
            ]
        },
    )


def v07_manifest(tmp_path):
    root = tmp_path / "runtime"
    raw = root / "evidence/inv-trace/1.json"
    raw.parent.mkdir(parents=True)
    raw.write_text("{}", encoding="utf-8")
    sections = v07_entries(write_evidence(tmp_path))
    return manifest(
        [scenario("f01", [run("a")])],
        idempotency=sections["idempotency"],
        dedup=sections["dedup"],
        trace=sections["trace"],
        evidence_root=str(root),
    )


def test_v07_passes_with_single_attempt_deduped_alert_and_raw_reference(tmp_path):
    report = check(tmp_path, v07_manifest(tmp_path), v07_store())

    assert criterion(report, "V07").passed is True


def test_v07_rejects_two_approval_records(tmp_path):
    store = v07_store()
    store._approval_counts["act-once"] = 2

    v07 = criterion(check(tmp_path, v07_manifest(tmp_path), store), "V07")
    assert v07.passed is False
    assert "审批记录 2 条" in v07.detail


def test_v07_rejects_a_second_incident_inside_the_window(tmp_path):
    store = v07_store()
    stamp = store._incident_created["inc-1"]
    store._receipts["fp-1"] = [stamp, stamp + timedelta(minutes=5)]

    v07 = criterion(check(tmp_path, v07_manifest(tmp_path), store), "V07")
    assert v07.passed is False
    assert "窗口内事件 2 个" in v07.detail


def test_v07_rejects_a_missing_raw_reference(tmp_path):
    payload = v07_manifest(tmp_path)
    for raw in (tmp_path / "runtime").rglob("*.json"):
        raw.unlink()

    v07 = criterion(check(tmp_path, payload, v07_store()), "V07")
    assert v07.passed is False
    assert "原始引用不存在或越界" in v07.detail


def test_v07_rejects_evidence_without_an_observation(tmp_path):
    store = v07_store()
    store._evidence["inv-trace"][0].observation = ""

    v07 = criterion(check(tmp_path, v07_manifest(tmp_path), store), "V07")
    assert v07.passed is False
    assert "没有观测内容" in v07.detail


def test_v07_requires_a_cluster_observation(tmp_path):
    store = v07_store()
    store._evidence["inv-trace"][0].source = "knowledge"
    store._evidence["inv-trace"][0].raw_ref = ""

    v07 = criterion(check(tmp_path, v07_manifest(tmp_path), store), "V07")
    assert v07.passed is False
    assert "没有集群观测证据" in v07.detail


def test_v07_rejects_an_absolute_raw_reference(tmp_path):
    store = v07_store()
    store._evidence["inv-trace"][0].raw_ref = "/etc/hostname"

    v07 = criterion(check(tmp_path, v07_manifest(tmp_path), store), "V07")
    assert v07.passed is False
    assert "不是相对路径" in v07.detail


def test_v07_rejects_an_unfinished_tool_call(tmp_path):
    store = v07_store()
    store._tool_calls["inv-trace"][0].finished_at = None

    v07 = criterion(check(tmp_path, v07_manifest(tmp_path), store), "V07")
    assert v07.passed is False
    assert "工具调用未完成" in v07.detail


def test_repository_manifest_keeps_trace_evidence_inside_the_repository():
    """回查用例的原始文件要随代码入库，不能只放在被忽略的本地运行目录。"""

    from pathlib import Path

    from aiops.eval import load_manifest

    root = Path(__file__).resolve().parents[1]
    loaded = load_manifest(root / "eval/scenarios.json")

    evidence_root = (root / loaded.evidence_root).resolve()
    assert evidence_root.is_relative_to(root), loaded.evidence_root
    assert evidence_root.is_dir(), loaded.evidence_root
    assert loaded.trace_runs, "清单缺少回查用例"
    for item in loaded.trace_runs:
        assert (root / item.evidence).is_file(), item.evidence
