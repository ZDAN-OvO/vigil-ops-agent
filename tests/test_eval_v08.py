"""V08：Context 可被反证，runbook 有取舍，追问只读且引用已有证据。"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from eval_fakes import (
    FakeStore,
    ProposalStatus,
    approval_for,
    audit,
    check,
    criterion,
    investigation,
    manifest,
    proposal_obj,
    run,
    scenario,
    write_evidence,
)

from aiops.contracts import Hypothesis
from aiops.investigation.snapshot import _digest, _hypothesis_digest, state_snapshot


def test_legacy_hypothesis_digest_ignores_new_revision_fields():
    """旧调查的 v2 快照不能因为新增字段而全部失配。"""
    item = Hypothesis(
        id="hyp-legacy",
        investigation_id="inv-legacy",
        statement="节点磁盘写满",
        expected_observations=["磁盘可用空间接近 0"],
        refutation_criteria=["磁盘指标正常"],
        revision=4,
    )
    legacy = _digest(
        {
            "statement": "节点磁盘写满",
            "status": "pending",
            "expected_observations": ["磁盘可用空间接近 0"],
        }
    )
    assert _hypothesis_digest(item) == legacy
    assert _hypothesis_digest(item, modern=True) != legacy


def v08_entries(evidence):
    return {
        "context": [
            {
                "run_id": "ctx",
                "investigation_id": "inv-ctx",
                "context_hint": "节点磁盘写满",
                "refuted_hypothesis": "磁盘",
                "evidence": evidence,
                "markers": ["ok"],
            }
        ],
        "runbooks": [
            {
                "run_id": "rb",
                "investigation_id": "inv-rb",
                "accepted": "rb-oom",
                "rejected": ["rb-dns"],
                "proposal_id": "act-rb",
                "evidence": evidence,
                "markers": ["ok"],
            }
        ],
        "explain": [
            {
                "run_id": "explain",
                "investigation_id": "inv-exp",
                "evidence": evidence,
                "markers": ["ok"],
            }
        ],
    }


def v08_store():
    def evidence(**overrides):
        base = {
            "source": "kubernetes",
            "relation": "supporting",
            "observation": "observed",
            "raw_ref": "evidence/1.json",
            "tool_call_id": "tc-1",
            "hypothesis_id": "hyp-oom",
        }
        base.update(overrides)
        return SimpleNamespace(**base)

    ctx_hypothesis = SimpleNamespace(
        id="hyp-disk",
        statement="节点磁盘写满导致服务异常",
        status=SimpleNamespace(value="refuted"),
    )
    exp_proposal = proposal_obj(
        "act-exp",
        ProposalStatus.PENDING_APPROVAL,
        "w1",
        investigation_id="inv-exp",
        created_at=datetime.now(UTC) - timedelta(hours=2),
    )
    approval = approval_for(exp_proposal)
    approval.decided_at = datetime.now(UTC) - timedelta(hours=2)
    store = FakeStore(
        proposals={
            "act-rb": proposal_obj(
                "act-rb", ProposalStatus.PENDING_APPROVAL, "w1", evidence_ids=["ev-k"]
            ),
            "act-exp": exp_proposal,
        },
        approvals={"act-exp": approval},
        investigations={
            "inv-ctx": investigation("inv-ctx"),
            "inv-rb": investigation("inv-rb"),
            "inv-exp": investigation("inv-exp"),
        },
        incidents={
            "inc-inv-ctx": SimpleNamespace(context=["有人怀疑是节点磁盘写满导致"]),
            "inc-inv-rb": SimpleNamespace(context=[]),
            "inc-inv-exp": SimpleNamespace(context=[]),
        },
        hypotheses={"inv-ctx": [ctx_hypothesis]},
        evidence={
            "inv-ctx": [evidence(id="ev-ctx", relation="refuting", hypothesis_id="hyp-disk")],
            "inv-rb": [evidence(id="ev-k")],
            "inv-exp": [evidence(id="ev-e")],
        },
        tool_calls={
            "inv-exp": [
                SimpleNamespace(
                    id="tc-1", started_at=datetime.now(UTC) - timedelta(hours=1), error_code=None
                )
            ]
        },
        runbooks={"rb-oom": SimpleNamespace(id="rb-oom"), "rb-dns": SimpleNamespace(id="rb-dns")},
        audits={
            "inv-rb": [
                audit("runbook.accepted", payload={"runbook_id": "rb-oom", "reason": "观测相符"}),
                audit("runbook.rejected", payload={"runbook_id": "rb-dns", "reason": "条件不符"}),
            ]
        },
    )
    snapshot = asyncio.run(
        state_snapshot(store, investigation("inv-exp"), [], store._evidence["inv-exp"])
    )
    store._audits["inv-exp"] = [
        audit("investigation.completed", seconds_ago=3600),
        audit(
            "explain.requested",
            seconds_ago=1800,
            payload={"request_id": "req-1", "snapshot": snapshot},
        ),
        audit(
            "explain.answered",
            seconds_ago=1790,
            payload={
                "request_id": "req-1",
                "citations": ["ev-e"],
                "needs_new_query": False,
                "snapshot": snapshot,
            },
        ),
        audit(
            "explain.requested",
            seconds_ago=1200,
            payload={"request_id": "req-2", "snapshot": snapshot},
        ),
        audit(
            "explain.answered",
            seconds_ago=1190,
            payload={
                "request_id": "req-2",
                "citations": [],
                "missing": ["审批记录"],
                "needs_new_query": True,
                "snapshot": snapshot,
            },
        ),
        audit(
            "explain.rejected",
            seconds_ago=600,
            payload={
                "request_id": "req-1",
                "snapshot": snapshot,
                "reason": "missing_without_new_query",
                "citations": [],
                "missing": ["审批记录"],
                "needs_new_query": False,
            },
        ),
    ]
    return store


def v08_manifest(tmp_path):
    entries = v08_entries(write_evidence(tmp_path))
    return manifest(
        [scenario("f01", [run("a")])],
        context=entries["context"],
        runbooks=entries["runbooks"],
        explain=entries["explain"],
    )


def answer(store, index, payload, request_id):
    store._audits["inv-exp"][index] = audit(
        "explain.answered",
        seconds_ago=1790,
        payload={"request_id": request_id, **payload},
    )


def test_v08_passes_with_context_runbook_and_explain(tmp_path):
    report = check(tmp_path, v08_manifest(tmp_path), v08_store())

    assert criterion(report, "V08").passed is True


def test_v08_rejects_an_unrefuted_context_hypothesis(tmp_path):
    store = v08_store()
    store._hypotheses["inv-ctx"] = [
        SimpleNamespace(
            id="hyp-disk",
            statement="节点磁盘写满导致服务异常",
            status=SimpleNamespace(value="supported"),
        )
    ]

    v08 = criterion(check(tmp_path, v08_manifest(tmp_path), store), "V08")
    assert v08.passed is False
    assert "假设没有被反驳记录" in v08.detail


def test_v08_rejects_refuting_evidence_that_points_to_another_hypothesis(tmp_path):
    store = v08_store()
    store._evidence["inv-ctx"][0].hypothesis_id = "hyp-other"

    v08 = criterion(check(tmp_path, v08_manifest(tmp_path), store), "V08")
    assert v08.passed is False
    assert "反驳证据没有指向该假设" in v08.detail


def test_v08_rejects_a_runbook_used_as_action_evidence(tmp_path):
    store = v08_store()
    store._evidence["inv-rb"] = [
        SimpleNamespace(
            id="ev-k",
            source="knowledge",
            relation="supporting",
            observation="runbook",
            raw_ref="",
            tool_call_id="tc-1",
            hypothesis_id="hyp-oom",
        )
    ]

    v08 = criterion(check(tmp_path, v08_manifest(tmp_path), store), "V08")
    assert v08.passed is False
    assert "runbook 进入动作证据" in v08.detail


def test_v08_rejects_an_explain_citation_outside_the_evidence(tmp_path):
    store = v08_store()
    answer(
        store,
        2,
        {
            "citations": ["ev-missing"],
            "needs_new_query": False,
            "snapshot": store._audits["inv-exp"][1].payload["snapshot"],
        },
        "req-1",
    )

    v08 = criterion(check(tmp_path, v08_manifest(tmp_path), store), "V08")
    assert v08.passed is False
    assert "引用不在证据中" in v08.detail


def test_v08_rejects_answers_without_any_citation(tmp_path):
    store = v08_store()
    answer(
        store,
        2,
        {
            "citations": [],
            "needs_new_query": True,
            "snapshot": store._audits["inv-exp"][1].payload["snapshot"],
        },
        "req-1",
    )

    v08 = criterion(check(tmp_path, v08_manifest(tmp_path), store), "V08")
    assert v08.passed is False
    assert "没有带引用的成功回答" in v08.detail


def test_v08_rejects_a_tool_call_after_explain(tmp_path):
    store = v08_store()
    store._tool_calls["inv-exp"].append(
        SimpleNamespace(
            id="tc-late",
            started_at=datetime.now(UTC) - timedelta(minutes=5),
            error_code=None,
        )
    )

    v08 = criterion(check(tmp_path, v08_manifest(tmp_path), store), "V08")
    assert v08.passed is False
    assert "追问后新增工具调用" in v08.detail


def test_v08_rejects_an_action_audit_under_a_linked_proposal(tmp_path):
    store = v08_store()
    store._proposals["act-late"] = proposal_obj(
        "act-late",
        ProposalStatus.SUCCEEDED,
        "w1",
        investigation_id="inv-exp",
        created_at=datetime.now(UTC) - timedelta(hours=1),
    )
    store._audits["act-late"] = [audit("action.succeeded", seconds_ago=60)]

    v08 = criterion(check(tmp_path, v08_manifest(tmp_path), store), "V08")
    assert v08.passed is False
    assert "追问后新增动作审计" in v08.detail


def test_v08_rejects_an_investigation_status_change_after_explain(tmp_path):
    store = v08_store()
    store._audits["inv-exp"].append(audit("investigation.running", seconds_ago=30))

    v08 = criterion(check(tmp_path, v08_manifest(tmp_path), store), "V08")
    assert v08.passed is False
    assert "追问后仍有动作或状态记录" in v08.detail


def test_v08_rejects_an_uncited_answer_without_a_new_query(tmp_path):
    store = v08_store()
    store._audits["inv-exp"].append(
        audit(
            "explain.answered",
            seconds_ago=300,
            payload={
                "citations": [],
                "missing": [],
                "needs_new_query": False,
                "snapshot": store._audits["inv-exp"][1].payload["snapshot"],
            },
        )
    )

    v08 = criterion(check(tmp_path, v08_manifest(tmp_path), store), "V08")
    assert v08.passed is False
    assert "存在没有引用的无据回答" in v08.detail


def test_v08_rejects_a_conclusion_change_after_explain(tmp_path):
    store = v08_store()
    changed = dict(store._audits["inv-exp"][1].payload["snapshot"])
    changed["conclusion_hash"] = "changed"
    answer(
        store,
        2,
        {"citations": ["ev-e"], "needs_new_query": False, "snapshot": changed},
        "req-1",
    )

    v08 = criterion(check(tmp_path, v08_manifest(tmp_path), store), "V08")
    assert v08.passed is False
    assert "追问前后状态快照不一致" in v08.detail


def test_v08_accepts_a_rejection_for_citations_outside_the_evidence(tmp_path):
    store = v08_store()
    store._audits["inv-exp"][5] = audit(
        "explain.rejected",
        seconds_ago=600,
        payload={
            "request_id": "req-1",
            "snapshot": store._audits["inv-exp"][1].payload["snapshot"],
            "unknown_citations": ["ev-404"],
            "citations": ["ev-404"],
        },
    )

    report = check(tmp_path, v08_manifest(tmp_path), store)

    assert criterion(report, "V08").passed is True


def test_v08_rejects_a_rejection_that_cites_existing_evidence(tmp_path):
    store = v08_store()
    store._audits["inv-exp"][5] = audit(
        "explain.rejected",
        seconds_ago=600,
        payload={
            "request_id": "req-1",
            "snapshot": store._audits["inv-exp"][1].payload["snapshot"],
            "unknown_citations": ["ev-e"],
            "citations": ["ev-e"],
        },
    )

    v08 = criterion(check(tmp_path, v08_manifest(tmp_path), store), "V08")
    assert v08.passed is False
    assert "拒绝记录与回答形态不一致" in v08.detail


def test_v08_rejects_a_rejection_without_a_recognised_cause(tmp_path):
    store = v08_store()
    store._audits["inv-exp"][5] = audit(
        "explain.rejected",
        seconds_ago=600,
        payload={
            "request_id": "req-1",
            "snapshot": store._audits["inv-exp"][1].payload["snapshot"],
            "reason": "whatever",
            "citations": [],
            "missing": [],
            "needs_new_query": False,
        },
    )

    v08 = criterion(check(tmp_path, v08_manifest(tmp_path), store), "V08")
    assert v08.passed is False
    assert "拒绝记录与回答形态不一致" in v08.detail


def test_v08_pairs_each_answer_with_its_own_request(tmp_path):
    store = v08_store()
    mutated = dict(store._audits["inv-exp"][1].payload["snapshot"])
    mutated["conclusion_hash"] = "mutated"
    store._audits["inv-exp"].insert(
        2,
        audit(
            "explain.requested",
            seconds_ago=1795,
            payload={"request_id": "req-x", "snapshot": mutated},
        ),
    )
    answer(
        store,
        3,
        {"citations": ["ev-e"], "needs_new_query": False, "snapshot": mutated},
        "req-1",
    )

    v08 = criterion(check(tmp_path, v08_manifest(tmp_path), store), "V08")
    assert v08.passed is False
    assert "追问前后状态快照不一致" in v08.detail


def test_v08_rejects_a_latest_answer_without_a_snapshot(tmp_path):
    store = v08_store()
    answer(
        store,
        4,
        {"citations": ["ev-e"], "missing": [], "needs_new_query": False},
        "req-2",
    )

    v08 = criterion(check(tmp_path, v08_manifest(tmp_path), store), "V08")
    assert v08.passed is False
    assert "回答缺少当前状态快照" in v08.detail


def test_v08_rejects_a_new_answer_without_a_request_id(tmp_path):
    store = v08_store()
    store._audits["inv-exp"].append(
        audit(
            "explain.answered",
            seconds_ago=300,
            payload={"citations": [], "missing": [], "needs_new_query": False},
        )
    )

    v08 = criterion(check(tmp_path, v08_manifest(tmp_path), store), "V08")
    assert v08.passed is False
    assert "回答没有配对的请求 ID" in v08.detail


def test_v08_rejects_a_modern_rejection_without_a_request_id(tmp_path):
    store = v08_store()
    store._audits["inv-exp"][5] = audit(
        "explain.rejected", seconds_ago=600, payload={"reason": "missing_without_new_query"}
    )

    v08 = criterion(check(tmp_path, v08_manifest(tmp_path), store), "V08")
    assert v08.passed is False
    assert "拒绝记录没有配对的请求 ID" in v08.detail


def test_v08_rejects_a_rejection_that_changed_the_state(tmp_path):
    store = v08_store()
    changed = dict(store._audits["inv-exp"][1].payload["snapshot"])
    changed["conclusion_hash"] = "mutated"
    store._audits["inv-exp"][5] = audit(
        "explain.rejected",
        seconds_ago=600,
        payload={
            "request_id": "req-1",
            "snapshot": changed,
            "reason": "unsupported_answer",
            "citations": [],
            "missing": [],
            "needs_new_query": False,
        },
    )

    v08 = criterion(check(tmp_path, v08_manifest(tmp_path), store), "V08")
    assert v08.passed is False
    assert "拒绝前后状态快照不一致" in v08.detail


def test_v08_rejects_a_missing_answer_without_listing_what_is_missing(tmp_path):
    store = v08_store()
    answer(
        store,
        4,
        {
            "citations": [],
            "missing": [],
            "needs_new_query": True,
            "snapshot": store._audits["inv-exp"][1].payload["snapshot"],
        },
        "req-1",
    )

    v08 = criterion(check(tmp_path, v08_manifest(tmp_path), store), "V08")
    assert v08.passed is False
    assert "存在没有引用的无据回答" in v08.detail


def test_v08_rejects_an_authorization_change_after_explain(tmp_path):
    store = v08_store()
    store._approvals["act-exp"].expires_at = datetime.now(UTC) + timedelta(days=30)

    v08 = criterion(check(tmp_path, v08_manifest(tmp_path), store), "V08")
    assert v08.passed is False
    assert "追问后结论、证据或授权已变化" in v08.detail


def test_v08_requires_a_comparable_snapshot(tmp_path):
    store = v08_store()
    for event in store._audits["inv-exp"]:
        if isinstance(event.payload.get("snapshot"), dict):
            event.payload["snapshot"] = {"conclusion_hash": "legacy"}

    v08 = criterion(check(tmp_path, v08_manifest(tmp_path), store), "V08")
    assert v08.passed is False
    assert "没有带请求 ID 的追问" in v08.detail
