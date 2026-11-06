"""V05：审批跨进程恢复，过期与漂移授权不得复用。"""

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
    proposal_hash,
    proposal_obj,
    run,
    scenario,
    write_evidence,
)


def v05_entries(evidence):
    return [
        {
            "run_id": "resume",
            "expected": "resumed",
            "proposal_id": "act-resume",
            "attempt_id": "att-resume",
            "evidence": evidence,
            "markers": ["ok"],
        },
        {
            "run_id": "expired",
            "expected": "expired",
            "proposal_id": "act-expired",
            "evidence": evidence,
            "markers": ["ok"],
        },
        {
            "run_id": "rejected",
            "expected": "rejected",
            "proposal_id": "act-rejected",
            "evidence": evidence,
            "markers": ["ok"],
        },
    ]


def v05_store():
    resumed = proposal_obj("act-resume", ProposalStatus.SUCCEEDED, "w1")
    expired = proposal_obj("act-expired", ProposalStatus.EXPIRED, "w1")
    rejected = proposal_obj("act-rejected", ProposalStatus.REJECTED, "w1")
    return FakeStore(
        proposals={
            "act-resume": resumed,
            "act-expired": expired,
            "act-rejected": rejected,
        },
        attempts={
            "act-resume": attempt(
                "verified",
                "att-resume",
                started_seconds=590,
                finished_seconds=505,
                snapshot={"approval_hash": proposal_hash(resumed)},
            )
        },
        approvals={
            "act-resume": approval_for(resumed),
            "act-expired": approval_for(expired, expires_seconds=-60),
            "act-rejected": approval_for(rejected, decision="rejected"),
        },
        audits={
            "act-resume": [
                audit(
                    "action.approved", seconds_ago=599, payload={"approval_id": "apr-act-resume"}
                ),
                audit("action.resumed", seconds_ago=595, payload={"approval_id": "apr-act-resume"}),
                audit("action.succeeded", seconds_ago=500, payload={"attempt_id": "att-resume"}),
            ],
            "act-expired": [
                audit(
                    "action.approved", seconds_ago=599, payload={"approval_id": "apr-act-expired"}
                ),
                audit("action.resumed", seconds_ago=30, payload={"approval_id": "apr-act-expired"}),
                audit("action.expired", seconds_ago=29),
            ],
            "act-rejected": [audit("action.rejected", seconds_ago=30)],
        },
    )


def payload(tmp_path):
    return manifest([scenario("f01", [run("a")])], approvals=v05_entries(write_evidence(tmp_path)))


def test_v05_passes_with_resumed_expired_and_rejected_cases(tmp_path):
    v05 = criterion(check(tmp_path, payload(tmp_path), v05_store()), "V05")

    assert v05.passed is True
    assert "缺用例 无" in v05.detail


def test_v05_rejects_an_expired_approval_that_executed(tmp_path):
    store = v05_store()
    store._attempts["act-expired"] = attempt("failed", "att-expired")

    v05 = criterion(check(tmp_path, payload(tmp_path), store), "V05")
    assert v05.passed is False
    assert "过期授权仍执行" in v05.detail


def test_v05_rejects_an_approval_that_does_not_match_the_proposal(tmp_path):
    store = v05_store()
    store._approvals["act-resume"] = SimpleNamespace(
        id="apr-act-resume",
        proposal_hash="stale",
        actor="reviewer",
        decision="approved",
        decided_at=datetime.now(UTC) - timedelta(minutes=20),
        expires_at=datetime.now(UTC) + timedelta(minutes=20),
    )

    v05 = criterion(check(tmp_path, payload(tmp_path), store), "V05")
    assert v05.passed is False
    assert "审批没有绑定当前提案" in v05.detail


def test_v05_rejects_an_expiry_that_had_not_passed(tmp_path):
    store = v05_store()
    store._approvals["act-expired"] = SimpleNamespace(
        id="apr-act-expired",
        proposal_hash=proposal_hash(store._proposals["act-expired"]),
        actor="reviewer",
        decision="approved",
        decided_at=datetime.now(UTC) - timedelta(seconds=600),
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )

    v05 = criterion(check(tmp_path, payload(tmp_path), store), "V05")
    assert v05.passed is False
    assert "继续发生在截止时间之前" in v05.detail


def test_v05_rejects_a_resume_audit_after_execution(tmp_path):
    store = v05_store()
    store._audits["act-resume"][1] = audit(
        "action.resumed", seconds_ago=580, payload={"approval_id": "apr-act-resume"}
    )

    v05 = criterion(check(tmp_path, payload(tmp_path), store), "V05")
    assert v05.passed is False
    assert "继续命令与执行顺序不符" in v05.detail


def test_v05_rejects_a_failed_execution(tmp_path):
    store = v05_store()
    store._attempts["act-resume"] = attempt(
        "failed",
        "att-resume",
        started_seconds=590,
        finished_seconds=505,
        snapshot={"approval_hash": proposal_hash(store._proposals["act-resume"])},
    )

    v05 = criterion(check(tmp_path, payload(tmp_path), store), "V05")
    assert v05.passed is False
    assert "执行终态=failed" in v05.detail


def test_v05_rejects_an_execution_bound_to_another_approval(tmp_path):
    store = v05_store()
    store._attempts["act-resume"] = attempt(
        "verified",
        "att-resume",
        started_seconds=590,
        finished_seconds=505,
        snapshot={"approval_hash": "stale"},
    )

    v05 = criterion(check(tmp_path, payload(tmp_path), store), "V05")
    assert v05.passed is False
    assert "执行没有绑定该授权" in v05.detail


def test_v05_rejects_a_success_audit_before_execution_finished(tmp_path):
    store = v05_store()
    store._audits["act-resume"][2] = audit(
        "action.succeeded",
        seconds_ago=550,
        payload={"attempt_id": "att-resume"},
    )

    v05 = criterion(check(tmp_path, payload(tmp_path), store), "V05")
    assert v05.passed is False
    assert "成功审计早于执行结束" in v05.detail
