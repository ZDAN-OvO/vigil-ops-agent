"""调查循环：结论必须有证据支撑，查询失败不得变成证据。"""

import asyncio

import psycopg
import pytest
from investigation_support import (
    DSN,
    ScriptedModel,
    ScriptedToolbox,
    failure_result,
    query,
    run_case,
    success_result,
)

from aiops.contracts import (
    InvestigationStatus,
)
from aiops.investigation.schemas import (
    Assessment,
    HypothesisDraft,
    HypothesisUpdate,
    PlanRequest,
    Verdict,
)

pytestmark = pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL，跳过真实库测试")


def test_supported_hypothesis_becomes_root_cause(ids, tmp_path):
    model = ScriptedModel(
        [
            PlanRequest(
                hypotheses=[
                    HypothesisDraft(
                        statement="坏发布导致首页失败",
                        expected_observations=["probe_success 为 0"],
                        rationale="首页报错且新版本刚发布",
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
                conclusion="新版本导致 productcatalogservice 不可用，首页返回 500",
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
    report, evidence, calls, saved = asyncio.run(
        run_case(ids, model, ScriptedToolbox([success_result()]), tmp_path=tmp_path)
    )

    assert report.status is InvestigationStatus.COMPLETED
    assert saved.status is InvestigationStatus.COMPLETED
    assert "productcatalogservice" in (saved.conclusion or "")
    assert [item.relation for item in evidence] == ["supporting"]
    assert len(calls) == 1 and calls[0].error_code is None
    assert report.hypotheses[0]["status"] == "supported"


def test_failed_query_is_not_turned_into_evidence(ids, tmp_path):
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
                hypotheses=[HypothesisUpdate(index=0, status="inconclusive", rationale="查询失败")],
                verdicts=[],
                next_queries=[],
                finish=True,
                root_cause=False,
                conclusion="取证失败，无法判断",
                action_kind="none",
                action_deployment="",
                action_pod="",
                action_service="",
                action_path="",
                action_reason="",
                action_evidence=[],
                runbook_uses=[],
            ),
        ]
    )
    report, evidence, calls, saved = asyncio.run(
        run_case(ids, model, ScriptedToolbox([failure_result()]), tmp_path=tmp_path)
    )

    assert saved.status is InvestigationStatus.INCONCLUSIVE
    assert evidence == []
    assert len(calls) == 1 and calls[0].error_code == "timeout"
    assert saved.conclusion is None
    assert "证据不足" in (saved.termination_reason or "")


def test_model_budget_exhaustion_aborts_instead_of_guessing(ids, tmp_path):
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
                queries=[],
            )
        ]
    )
    report, evidence, calls, saved = asyncio.run(
        run_case(
            ids,
            model,
            ScriptedToolbox([]),
            budget_model_calls=1,
            tmp_path=tmp_path,
        )
    )

    assert saved.status is InvestigationStatus.ABORTED
    assert "model_calls" in (saved.termination_reason or "")
    assert evidence == [] and calls == []


def test_checkpoint_is_persisted_for_the_thread(ids, tmp_path):
    model = ScriptedModel(
        [
            PlanRequest(
                hypotheses=[
                    HypothesisDraft(
                        statement="坏代理导致探测失败",
                        expected_observations=["probe_success 为 0"],
                        rationale="探测失败",
                    )
                ],
                queries=[],
            ),
            Assessment(
                hypotheses=[],
                verdicts=[],
                next_queries=[],
                finish=True,
                root_cause=False,
                conclusion="没有可用证据",
                action_kind="none",
                action_deployment="",
                action_pod="",
                action_service="",
                action_path="",
                action_reason="",
                action_evidence=[],
                runbook_uses=[],
            ),
        ]
    )
    asyncio.run(run_case(ids, model, ScriptedToolbox([]), tmp_path=tmp_path))

    with psycopg.connect(DSN) as conn:
        count = conn.execute(
            "SELECT count(*) FROM checkpoints WHERE thread_id = %s", (ids["investigation"],)
        ).fetchone()[0]
    assert count > 0


def test_model_call_never_exceeds_remaining_wall_clock(ids, tmp_path):
    """墙钟是硬截止：单次模型调用的超时必须压到剩余时间以内。"""

    model = ScriptedModel(
        [
            PlanRequest(
                hypotheses=[
                    HypothesisDraft(statement="x", expected_observations=["y"], rationale="z")
                ],
                queries=[],
            )
        ]
    )
    asyncio.run(
        run_case(
            ids,
            model,
            ScriptedToolbox([]),
            budget_model_calls=1,
            budget_tool_calls=1,
            tmp_path=tmp_path,
        )
    )

    assert model.timeouts, "没有发生模型调用"
    assert all(item is not None for item in model.timeouts)
    assert all(0 < item <= 600 for item in model.timeouts)
