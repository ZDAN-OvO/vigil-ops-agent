"""runbook 与 knowledge：未知或未判断的引用直接中止。"""

import asyncio

import psycopg
import pytest
from investigation_support import (
    DSN,
    FakeProposer,
    ScriptedModel,
    ScriptedToolbox,
    knowledge_result,
    query,
    run_case,
)

from aiops.investigation.schemas import (
    Assessment,
    HypothesisDraft,
    HypothesisUpdate,
    PlanRequest,
    RunbookUse,
    Verdict,
)

pytestmark = pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL，跳过真实库测试")


def test_runbook_use_is_recorded(ids, tmp_path):
    proposer = FakeProposer()
    model = ScriptedModel(
        [
            PlanRequest(
                hypotheses=[
                    HypothesisDraft(
                        statement="依赖解析失败",
                        expected_observations=["日志出现解析错误"],
                        rationale="报错文本",
                    )
                ],
                queries=[query(tool="knowledge.list_runbooks")],
            ),
            Assessment(
                hypotheses=[HypothesisUpdate(index=0, status="supported", rationale="证据")],
                verdicts=[Verdict(evidence_index=0, relation="supporting")],
                next_queries=[],
                finish=True,
                root_cause=True,
                conclusion="按 runbook rb-oom-limit 的顺序核对",
                action_kind="none",
                action_deployment="",
                action_pod="",
                action_service="",
                action_path="",
                action_reason="",
                action_evidence=[],
                runbook_uses=[
                    RunbookUse(runbook_id="rb-oom-limit", accepted=True, reason="与证据相符")
                ],
            ),
        ]
    )
    report, _, _, _ = asyncio.run(
        run_case(
            ids, model, ScriptedToolbox([knowledge_result()]), tmp_path=tmp_path, proposer=proposer
        )
    )

    assert [use["runbook_id"] for use in report.runbook_uses] == ["rb-oom-limit"]
    assert report.runbook_uses[0]["accepted"] is True
    with psycopg.connect(DSN) as conn:
        events = [
            row[0]
            for row in conn.execute(
                "SELECT event FROM audit_events WHERE correlation_id = %s", (ids["investigation"],)
            )
        ]
    assert "runbook.accepted" in events


def _runbook_assessment(**overrides) -> Assessment:
    values = {
        "hypotheses": [HypothesisUpdate(index=0, status="supported", rationale="证据")],
        "verdicts": [Verdict(evidence_index=0, relation="supporting")],
        "next_queries": [],
        "finish": True,
        "root_cause": True,
        "conclusion": "按条目核对",
        "action_kind": "none",
        "action_deployment": "",
        "action_pod": "",
        "action_service": "",
        "action_path": "",
        "action_reason": "",
        "action_evidence": [],
        "runbook_uses": [],
    }
    values.update(overrides)
    return Assessment(**values)


def _run_knowledge_case(ids, tmp_path, *assessments):
    model = ScriptedModel(
        [
            PlanRequest(
                hypotheses=[
                    HypothesisDraft(statement="x", expected_observations=["y"], rationale="z")
                ],
                queries=[query(tool="knowledge.list_runbooks")],
            ),
            *assessments,
        ]
    )
    return asyncio.run(
        run_case(ids, model, ScriptedToolbox([knowledge_result()]), tmp_path=tmp_path)
    )


def test_unknown_runbook_citation_gets_one_repair_round(ids, tmp_path):
    report, _, _, _ = _run_knowledge_case(
        ids,
        tmp_path,
        _runbook_assessment(
            runbook_uses=[RunbookUse(runbook_id="rb-ghost", accepted=True, reason="猜的")]
        ),
        _runbook_assessment(
            runbook_uses=[RunbookUse(runbook_id="rb-oom-limit", accepted=True, reason="吻合")]
        ),
    )

    assert report.status.value != "aborted"
    assert "rb-ghost" not in (report.termination_reason or "")


def test_unknown_runbook_citation_aborts_after_the_repair_round(ids, tmp_path):
    bad = _runbook_assessment(
        runbook_uses=[RunbookUse(runbook_id="rb-ghost", accepted=True, reason="猜的")]
    )
    report, _, _, _ = _run_knowledge_case(ids, tmp_path, bad, bad)

    assert report.status.value == "aborted"
    assert "rb-ghost" in (report.termination_reason or "")


def test_unjudged_runbook_candidate_aborts_the_investigation(ids, tmp_path):
    report, _, _, _ = _run_knowledge_case(ids, tmp_path, _runbook_assessment(runbook_uses=[]))

    assert report.status.value == "aborted"
    assert "rb-oom-limit" in (report.termination_reason or "")


def test_knowledge_only_conclusion_is_not_accepted(ids, tmp_path):
    model = ScriptedModel(
        [
            PlanRequest(
                hypotheses=[
                    HypothesisDraft(
                        statement="内存不足", expected_observations=["集群证据"], rationale="x"
                    )
                ],
                queries=[query()],
            ),
            _runbook_assessment(conclusion="根据 rb-oom-limit 已确认内存不足"),
        ]
    )
    report, _, _, _ = asyncio.run(
        run_case(ids, model, ScriptedToolbox([knowledge_result()]), tmp_path=tmp_path)
    )

    assert report.status.value == "inconclusive"
    assert report.conclusion is None


def test_knowledge_evidence_cannot_support_an_action(ids, tmp_path):
    proposer = FakeProposer()
    model = ScriptedModel(
        [
            PlanRequest(
                hypotheses=[
                    HypothesisDraft(statement="x", expected_observations=["y"], rationale="z")
                ],
                queries=[query()],
            ),
            _runbook_assessment(
                action_kind="rollout_undo",
                action_deployment="productcatalogservice",
                action_evidence=[0],
            ),
        ]
    )
    report, _, _, _ = asyncio.run(
        run_case(
            ids,
            model,
            ScriptedToolbox([knowledge_result()]),
            tmp_path=tmp_path,
            proposer=proposer,
        )
    )

    assert report.proposal_id is None
    assert proposer.calls == []
