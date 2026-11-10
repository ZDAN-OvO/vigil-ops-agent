"""调查提案：动作必须有支撑证据并在授权边界内。"""

import asyncio

import pytest
from investigation_support import (
    DSN,
    FakeProposer,
    ScriptedModel,
    ScriptedToolbox,
    query,
    run_case,
    success_result,
)

from aiops.actions.executor import AutoExecutionError
from aiops.contracts import (
    ActionOrigin,
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


def test_model_proposal_is_created_with_supporting_evidence(ids, tmp_path):
    proposer = FakeProposer()
    model = ScriptedModel(
        [
            PlanRequest(
                hypotheses=[
                    HypothesisDraft(
                        statement="坏发布导致首页失败",
                        expected_observations=["probe_success 为 0"],
                        rationale="首页报错",
                    )
                ],
                queries=[query()],
            ),
            Assessment(
                hypotheses=[HypothesisUpdate(index=0, status="supported", rationale="探测为 0")],
                verdicts=[Verdict(evidence_index=0, relation="supporting")],
                next_queries=[],
                finish=True,
                root_cause=True,
                conclusion="productcatalogservice 新版本导致首页 500",
                action_kind="rollout_undo",
                action_deployment="productcatalogservice",
                action_pod="",
                action_service="",
                action_path="",
                action_reason="回退到上一版本",
                action_evidence=[0],
                runbook_uses=[],
            ),
        ]
    )
    report, evidence, _, _ = asyncio.run(
        run_case(
            ids,
            model,
            ScriptedToolbox([success_result()]),
            tmp_path=tmp_path,
            proposer=proposer,
        )
    )

    assert report.proposal_id == "act-model-1"
    assert len(proposer.calls) == 1
    call = proposer.calls[0]
    assert call["origin"] is ActionOrigin.MODEL
    assert call["evidence_ids"] == [evidence[0].id]
    assert call["investigation_id"] == ids["investigation"]
    assert call["deployment"] == "productcatalogservice"


def test_restart_pod_assessment_proposes_pod_restart(ids, tmp_path):
    proposer = FakeProposer()
    model = ScriptedModel(
        [
            PlanRequest(
                hypotheses=[
                    HypothesisDraft(
                        statement="Pod 级状态损坏导致业务请求失败",
                        expected_observations=["Pod 内请求全部失败"],
                        rationale="应用日志持续 500",
                    )
                ],
                queries=[query()],
            ),
            Assessment(
                hypotheses=[HypothesisUpdate(index=0, status="supported", rationale="失败持续")],
                verdicts=[Verdict(evidence_index=0, relation="supporting")],
                next_queries=[],
                finish=True,
                root_cause=True,
                conclusion="重建该 Pod 可以清除状态",
                action_kind="restart_pod",
                action_deployment="",
                action_pod="rebuild-demo-abc",
                action_service="rebuild-demo",
                action_path="/work",
                action_reason="Pod 级状态损坏",
                action_evidence=[0],
                runbook_uses=[],
            ),
        ]
    )
    report, evidence, _, _ = asyncio.run(
        run_case(
            ids,
            model,
            ScriptedToolbox([success_result()]),
            tmp_path=tmp_path,
            proposer=proposer,
        )
    )

    assert report.proposal_id == "act-model-1"
    call = proposer.calls[0]
    assert call["kind"] == "restart_pod"
    assert call["pod"] == "rebuild-demo-abc"
    assert call["service"] == "rebuild-demo"
    assert call["path"] == "/work"
    assert call["evidence_ids"] == [evidence[0].id]


def _restart_assessment(**overrides) -> Assessment:
    values = {
        "hypotheses": [HypothesisUpdate(index=0, status="supported", rationale="失败持续")],
        "verdicts": [Verdict(evidence_index=0, relation="supporting")],
        "next_queries": [],
        "finish": True,
        "root_cause": True,
        "conclusion": "重建该 Pod 可以清除状态",
        "action_kind": "restart_pod",
        "action_deployment": "",
        "action_pod": "rebuild-demo-abc",
        "action_service": "rebuild-demo",
        "action_path": "/work",
        "action_reason": "Pod 级状态损坏",
        "action_evidence": [0],
        "runbook_uses": [],
    }
    values.update(overrides)
    return Assessment(**values)


def _run_restart_case(ids, tmp_path, proposer, *assessments):
    model = ScriptedModel(
        [
            PlanRequest(
                hypotheses=[
                    HypothesisDraft(
                        statement="Pod 级状态损坏",
                        expected_observations=["请求失败"],
                        rationale="日志",
                    )
                ],
                queries=[query()],
            ),
            *assessments,
        ]
    )
    return asyncio.run(
        run_case(
            ids, model, ScriptedToolbox([success_result()]), tmp_path=tmp_path, proposer=proposer
        )
    )


def test_auto_execution_failure_keeps_the_proposal_id(ids, tmp_path):
    """提案已落库但执行没完成时，报告不能把它当成“未提案”。"""

    class BusyProposer(FakeProposer):
        async def propose_restart_pod(self, **kwargs):
            raise AutoExecutionError("target_busy", "目标已有未完成的执行", "act-persisted-auto")

    report, _, _, _ = _run_restart_case(ids, tmp_path, BusyProposer(), _restart_assessment())

    assert report.proposal_id == "act-persisted-auto"


def test_unknown_runbook_aborts_before_any_proposal(ids, tmp_path):
    """结论先被 runbook 校验拒绝时，不能在 abort 前把动作提出去（自动允许会立即执行）。"""
    proposer = FakeProposer()

    bad = _restart_assessment(
        runbook_uses=[RunbookUse(runbook_id="rb-ghost", accepted=True, reason="猜的")]
    )
    report, _, _, _ = _run_restart_case(ids, tmp_path, proposer, bad, bad)

    assert report.status.value == "aborted"
    assert proposer.calls == []


def test_restart_without_root_cause_is_not_proposed(ids, tmp_path):
    proposer = FakeProposer()

    report, _, _, _ = _run_restart_case(
        ids, tmp_path, proposer, _restart_assessment(root_cause=False, conclusion="")
    )

    assert report.proposal_id is None
    assert proposer.calls == []


def test_restart_with_empty_conclusion_is_not_proposed(ids, tmp_path):
    """空结论最终会被判为 inconclusive，不能在这种状态下先执行动作。"""
    proposer = FakeProposer()

    report, _, _, _ = _run_restart_case(ids, tmp_path, proposer, _restart_assessment(conclusion=""))

    assert proposer.calls == []
    assert report.proposal_id is None


def test_restart_citing_refuting_evidence_is_not_proposed(ids, tmp_path):
    proposer = FakeProposer()
    assessment = _restart_assessment(
        verdicts=[Verdict(evidence_index=0, relation="refuting")],
        hypotheses=[HypothesisUpdate(index=0, status="refuted", rationale="请求正常")],
    )

    report, _, _, _ = _run_restart_case(ids, tmp_path, proposer, assessment)

    assert report.proposal_id is None
    assert proposer.calls == []


def test_restart_without_service_is_not_proposed(ids, tmp_path):
    proposer = FakeProposer()

    report, _, _, _ = _run_restart_case(
        ids, tmp_path, proposer, _restart_assessment(action_service="")
    )

    assert report.proposal_id is None
    assert proposer.calls == []


def test_action_without_supporting_evidence_is_not_proposed(ids, tmp_path):
    proposer = FakeProposer()
    model = ScriptedModel(
        [
            PlanRequest(
                hypotheses=[
                    HypothesisDraft(
                        statement="坏发布导致首页失败",
                        expected_observations=["probe_success 为 0"],
                        rationale="首页报错",
                    )
                ],
                queries=[query()],
            ),
            Assessment(
                hypotheses=[HypothesisUpdate(index=0, status="refuted", rationale="探测正常")],
                verdicts=[Verdict(evidence_index=0, relation="refuting")],
                next_queries=[],
                finish=True,
                root_cause=False,
                conclusion="证据不支持",
                action_kind="rollout_undo",
                action_deployment="productcatalogservice",
                action_pod="",
                action_service="",
                action_path="",
                action_reason="不该被执行",
                action_evidence=[],
                runbook_uses=[],
            ),
        ]
    )
    report, _, _, _ = asyncio.run(
        run_case(
            ids,
            model,
            ScriptedToolbox([success_result()]),
            tmp_path=tmp_path,
            proposer=proposer,
        )
    )

    assert report.proposal_id is None
    assert proposer.calls == []
