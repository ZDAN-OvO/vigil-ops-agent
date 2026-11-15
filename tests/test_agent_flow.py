"""单 Agent 只读闭环：假设、观测、证据关系、预算与终态判定。"""

import asyncio

import pytest
from db_env import DSN
from investigation_support import (
    FakeToolbox,
    agent_case,
    call,
    create_hypothesis,
    hypothesis_id,
    observation_ids,
    report_call,
    tool_payloads,
)

from aiops.contracts import (
    Budget,
    Hypothesis,
    InvestigationStatus,
    TaskOutcome,
    TerminationCode,
)

pytestmark = pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL，跳过真实库测试")

PROBE = {
    "expression": 'probe_success{job="frontend-probe"}',
    "lookback_seconds": 120,
    "step_seconds": 15,
}


def _probe_call(call_id: str = "call-probe"):
    return call("prometheus_query_range", PROBE, call_id)


def _assess(messages, *, relation: str = "supporting", obs_index: int = 0):
    return call(
        "assess_evidence",
        {
            "links": [
                {
                    "observation_id": observation_ids(messages)[obs_index],
                    "hypothesis_id": hypothesis_id(messages),
                    "relation": relation,
                    "rationale": "探测返回 0",
                }
            ],
            "hypothesis_updates": [
                {
                    "hypothesis_id": hypothesis_id(messages),
                    "status": "supported" if relation == "supporting" else "inconclusive",
                    "reason": "按探测结果更新",
                }
            ],
        },
        "call-assess",
    )


async def _count(store, investigation_id: str, table: str) -> int:
    async with store._pool.connection() as conn:
        cursor = await conn.execute(
            f"SELECT count(*) AS total FROM {table} WHERE investigation_id = %s",
            (investigation_id,),
        )
        row = await cursor.fetchone()
    return int(row["total"])


def test_agent_roundtrip_records_observation_relation_and_task(ids):
    responses = [
        create_hypothesis(),
        _probe_call(),
        lambda messages: _assess(messages),
        lambda messages: report_call(
            conclusion="坏发布导致首页 500",
            supporting=observation_ids(messages),
        ),
    ]

    async def run():
        async with agent_case(ids, responses=responses) as case:
            report = await case.runtime.run()
            observations = await case.store.list_observations(ids["investigation"])
            relations = await case.store.list_hypothesis_evidence(ids["investigation"])
            requests = await case.store.list_tool_requests(ids["investigation"])
            task = await case.store.get_task(ids["investigation"])
            return report, observations, relations, requests, task

    report, observations, relations, requests, task = asyncio.run(run())
    assert report.status is InvestigationStatus.COMPLETED
    assert report.task is not None and report.task["outcome"] == TaskOutcome.DIAGNOSIS_ONLY.value
    assert report.task["termination_code"] == TerminationCode.NO_APPLICABLE_ACTION.value
    assert "坏发布" in (report.conclusion or "")
    assert len(observations) == 1
    assert observations[0].status.value == "ok"
    assert [(item.relation, item.observation_id) for item in relations] == [
        ("supporting", observations[0].id)
    ]
    assert [item.state.value for item in requests] == ["observed", "observed", "observed"]
    assert {item.tool for item in requests} == {
        "manage_hypotheses",
        "prometheus.query_range",
        "assess_evidence",
    }
    assert task is not None
    assert task.phase.value == "terminal"
    assert task.used_tool_calls == 3
    assert task.used_model_calls == 4


def test_empty_observation_cannot_be_refuting(ids):
    empty = {"prometheus.query_range": {"source": "prometheus", "data": {"series": []}}}
    responses = [
        create_hypothesis(),
        _probe_call(),
        lambda messages: _assess(messages, relation="refuting"),
        lambda messages: report_call(confirmed=False, conclusion="没有取到数据"),
    ]

    async def run():
        async with agent_case(ids, responses=responses, toolbox=FakeToolbox(empty)) as case:
            report = await case.runtime.run()
            relations = await case.store.list_hypothesis_evidence(ids["investigation"])
            payloads = tool_payloads(case.model.seen[-1])
            return report, relations, payloads

    report, relations, payloads = asyncio.run(run())
    assert report.status is InvestigationStatus.INCONCLUSIVE
    assert relations == []
    assert any(
        (payload.get("error") or {}).get("code") == "not_real_evidence" for payload in payloads
    )


def test_failed_query_is_not_evidence(ids):
    failing = {"prometheus.query_range": TimeoutError("查询超时")}
    responses = [
        create_hypothesis(),
        _probe_call(),
        lambda messages: report_call(confirmed=False, conclusion="取证失败"),
    ]

    async def run():
        async with agent_case(ids, responses=responses, toolbox=FakeToolbox(failing)) as case:
            report = await case.runtime.run()
            observations = await case.store.list_observations(ids["investigation"])
            calls = await case.store.list_tool_calls(ids["investigation"])
            requests = await case.store.list_tool_requests(ids["investigation"])
            return report, observations, calls, requests

    report, observations, calls, requests = asyncio.run(run())
    assert observations == []
    assert [item.error_code for item in calls] == ["timeout"]
    assert any(item.state.value == "failed" for item in requests)
    assert report.status is InvestigationStatus.INCONCLUSIVE
    assert report.evidence_count == 0


def test_runbook_is_reference_not_evidence(ids):
    runbook = {
        "knowledge.list_runbooks": {
            "source": "knowledge",
            "data": {"items": [{"id": "rb-oom", "version": 1, "title": "OOM 参考"}]},
        }
    }
    responses = [
        create_hypothesis(),
        call(
            "knowledge_list_runbooks",
            {"query": "OOM", "service": "frontend"},
            "call-runbook",
        ),
        lambda messages: call(
            "assess_evidence",
            {
                "links": [
                    {
                        "observation_id": observation_ids(messages)[0],
                        "hypothesis_id": hypothesis_id(messages),
                        "relation": "supporting",
                        "rationale": "runbook 说要重启",
                    }
                ],
                "hypothesis_updates": [],
            },
            "call-assess",
        ),
        lambda messages: report_call(confirmed=False, conclusion="没有集群证据"),
    ]

    async def run():
        async with agent_case(ids, responses=responses, toolbox=FakeToolbox(runbook)) as case:
            report = await case.runtime.run()
            relations = await case.store.list_hypothesis_evidence(ids["investigation"])
            return report, relations, tool_payloads(case.model.seen[-1])

    report, relations, payloads = asyncio.run(run())
    assert relations == []
    assert any(
        (payload.get("error") or {}).get("code") == "knowledge_not_evidence" for payload in payloads
    )
    assert report.status is InvestigationStatus.INCONCLUSIVE


def test_invalid_citation_gets_one_correction_then_stops(ids):
    responses = [
        report_call(supporting=["obs-missing"], call_id="call-report-1"),
        report_call(supporting=["obs-missing"], call_id="call-report-2"),
    ]

    async def run():
        async with agent_case(ids, responses=responses) as case:
            return await case.runtime.run()

    report = asyncio.run(run())
    assert report.status is InvestigationStatus.INCONCLUSIVE
    assert report.task is not None
    assert report.task["termination_code"] == TerminationCode.INVALID_REPORT.value
    assert report.model_summary_completed is False


def test_no_progress_ends_inconclusive(ids):
    def failing_assess(index: int):
        return call(
            "assess_evidence",
            {
                "links": [
                    {
                        "observation_id": f"obs-missing-{index}",
                        "hypothesis_id": "hyp-missing",
                        "relation": "supporting",
                        "rationale": "重复失败的判定",
                    }
                ],
                "hypothesis_updates": [],
            },
            f"call-assess-{index}",
        )

    responses = [failing_assess(index) for index in range(6)]

    async def run():
        async with agent_case(ids, responses=responses) as case:
            return await case.runtime.run()

    report = asyncio.run(run())
    assert report.status is InvestigationStatus.INCONCLUSIVE
    assert report.task is not None
    assert report.task["termination_code"] == TerminationCode.NO_PROGRESS.value
    assert report.task["no_progress_rounds"] >= 3


def test_model_budget_exhaustion_aborts(ids):
    budget = Budget(
        max_tool_calls=10,
        max_model_calls=2,
        max_wall_clock_seconds=120,
        max_action_seconds=120,
    )
    responses = [create_hypothesis(), _probe_call(), _probe_call(call_id="call-probe-2")]

    async def run():
        async with agent_case(ids, responses=responses, budget=budget) as case:
            return await case.runtime.run()

    report = asyncio.run(run())
    assert report.status is InvestigationStatus.ABORTED
    assert report.task is not None
    assert report.task["termination_code"] == TerminationCode.BUDGET_EXHAUSTED.value
    assert report.task["used_model_calls"] == 2


def test_hypothesis_refine_refute_reopen_and_revision_history(ids):
    def refine(messages):
        return call(
            "manage_hypotheses",
            {
                "operations": [
                    {
                        "op": "refine",
                        "hypothesis_id": hypothesis_id(messages),
                        "statement": "细化：Pod 的 emptyDir 状态损坏",
                        "expected_observations": ["/work 500 且 Pod 就绪"],
                        "refutation_criteria": ["重建后仍然 500"],
                        "reason": "日志限定到 Pod 级状态",
                    }
                ]
            },
            "call-refine",
        )

    def refute(messages):
        return call(
            "manage_hypotheses",
            {
                "operations": [
                    {
                        "op": "update_status",
                        "hypothesis_id": hypothesis_id(messages),
                        "status": "refuted",
                        "reason": "重建后仍失败",
                    }
                ]
            },
            "call-refute",
        )

    def reopen(messages):
        return call(
            "manage_hypotheses",
            {
                "operations": [
                    {
                        "op": "update_status",
                        "hypothesis_id": hypothesis_id(messages),
                        "status": "pending",
                        "reason": "新证据说明重建没有真正发生",
                    }
                ]
            },
            "call-reopen",
        )

    responses = [
        create_hypothesis(),
        refine,
        refute,
        reopen,
        report_call(confirmed=False, conclusion="证据不足"),
    ]

    async def run():
        async with agent_case(ids, responses=responses) as case:
            report = await case.runtime.run()
            hypotheses = await case.store.list_hypotheses(ids["investigation"])
            revisions = await case.store.list_hypothesis_revisions(ids["investigation"])
            return report, hypotheses, revisions

    report, hypotheses, revisions = asyncio.run(run())
    assert report.status is InvestigationStatus.INCONCLUSIVE
    assert len(hypotheses) == 1
    assert hypotheses[0].revision == 4
    assert hypotheses[0].status.value == "pending"
    assert "emptyDir" in hypotheses[0].statement
    assert [row["revision"] for row in revisions] == [1, 2, 3, 4]
    assert [row["status"] for row in revisions] == ["pending", "pending", "refuted", "pending"]


def test_hypothesis_parent_must_exist_and_be_in_batch(ids):
    responses = [
        call(
            "manage_hypotheses",
            {
                "operations": [
                    {
                        "op": "create",
                        "temp_id": "parent",
                        "statement": "父假设",
                        "expected_observations": ["y"],
                        "refutation_criteria": ["z"],
                        "reason": "测试",
                    },
                    {
                        "op": "create",
                        "temp_id": "child",
                        "parent_id": "parent",
                        "statement": "子假设",
                        "expected_observations": ["y"],
                        "refutation_criteria": ["z"],
                        "reason": "测试",
                    },
                ]
            },
            "call-create",
        ),
        call(
            "manage_hypotheses",
            {
                "operations": [
                    {
                        "op": "create",
                        "temp_id": "x",
                        "parent_id": "hyp-does-not-exist",
                        "statement": "引用不存在父节点",
                        "expected_observations": ["y"],
                        "refutation_criteria": ["z"],
                        "reason": "测试",
                    }
                ]
            },
            "call-bad-parent",
        ),
        report_call(confirmed=False, conclusion="测试结束"),
    ]

    async def run():
        async with agent_case(ids, responses=responses) as case:
            report = await case.runtime.run()
            payloads = tool_payloads(case.model.seen[-1])
            hypotheses = await case.store.list_hypotheses(ids["investigation"])
            return report, payloads, hypotheses

    report, payloads, hypotheses = asyncio.run(run())
    assert any((payload.get("error") or {}).get("code") == "invalid_parent" for payload in payloads)
    assert report.status is InvestigationStatus.INCONCLUSIVE
    assert len(hypotheses) == 2
    child = next(item for item in hypotheses if item.parent_id is not None)
    assert child.parent_id in {item.id for item in hypotheses}


def test_cross_investigation_parent_is_rejected(ids):
    """父假设必须属于同一调查；跨调查引用直接拒绝。"""
    from investigation_support import ScriptedChatModel  # noqa: F401 - 仅用于类型说明
    from langchain.tools import ToolRuntime

    from aiops.agent.schemas import HypothesisOperation
    from aiops.agent.tools import manage_hypotheses

    async def run():
        async with agent_case(ids, responses=[]) as case:
            other_id = f"inv-other-{ids['investigation'][-6:]}"
            other_incident = case.incident.model_copy(update={"id": f"inc-other-{other_id[-6:]}"})
            await case.store.save_incident(other_incident)
            other = case.investigation.model_copy(
                update={"id": other_id, "incident_id": other_incident.id}
            )
            await case.store.create_investigation(other)
            other_hypothesis = Hypothesis(
                id=f"hyp-other-{other_id[-6:]}",
                investigation_id=other_id,
                statement="别的调查的假设",
                expected_observations=["x"],
            )
            await case.store.save_hypothesis(other_hypothesis)
            _, context = await case.runtime._open_context()
            fake_runtime = ToolRuntime(
                state={},
                context=context,
                config={},
                stream_writer=None,
                tool_call_id="call-cross",
                store=None,
            )
            try:
                message = await manage_hypotheses.coroutine(
                    operations=[
                        HypothesisOperation(
                            op="create",
                            parent_id=other_hypothesis.id,
                            statement="子假设",
                            expected_observations=["y"],
                            refutation_criteria=["z"],
                            reason="跨调查测试",
                        )
                    ],
                    runtime=fake_runtime,
                )
            finally:
                async with case.store._pool.connection() as conn:
                    await conn.execute(
                        "DELETE FROM hypothesis_revisions WHERE hypothesis_id = %s",
                        (other_hypothesis.id,),
                    )
                    await conn.execute(
                        "DELETE FROM hypotheses WHERE investigation_id = %s", (other_id,)
                    )
                    await conn.execute("DELETE FROM investigations WHERE id = %s", (other_id,))
                    await conn.execute("DELETE FROM incidents WHERE id = %s", (other_incident.id,))
            return message

    message = asyncio.run(run())
    assert "invalid_parent" in message
    assert "父假设不存在或不属于本次调查" in message


def test_hypothesis_parent_cycle_is_rejected(ids):
    """把已有假设挂到自己的子孙下必须被拒绝，不能形成环。"""
    from langchain.tools import ToolRuntime

    from aiops.agent.schemas import HypothesisOperation
    from aiops.agent.tools import manage_hypotheses

    async def run():
        async with agent_case(ids, responses=[]) as case:
            parent = Hypothesis(
                id=f"hyp-cycle-a-{ids['investigation'][-6:]}",
                investigation_id=ids["investigation"],
                statement="根假设",
                expected_observations=["x"],
            )
            await case.store.save_hypothesis(parent)
            child = Hypothesis(
                id=f"hyp-cycle-b-{ids['investigation'][-6:]}",
                investigation_id=ids["investigation"],
                parent_id=parent.id,
                statement="子假设",
                expected_observations=["y"],
            )
            await case.store.save_hypothesis(child)
            _, context = await case.runtime._open_context()
            fake_runtime = ToolRuntime(
                state={},
                context=context,
                config={},
                stream_writer=None,
                tool_call_id="call-cycle",
                store=None,
            )
            return await manage_hypotheses.coroutine(
                operations=[
                    HypothesisOperation(
                        op="refine",
                        hypothesis_id=parent.id,
                        parent_id=child.id,
                        reason="制造环",
                    )
                ],
                runtime=fake_runtime,
            )

    message = asyncio.run(run())
    assert "invalid_parent" in message
    assert "环" in message


def test_hypothesis_refine_can_move_to_another_parent(ids):
    """refine 可显式改父节点，并保留版本历史。"""
    from langchain.tools import ToolRuntime

    from aiops.agent.schemas import HypothesisOperation
    from aiops.agent.tools import manage_hypotheses

    async def run():
        async with agent_case(ids, responses=[]) as case:
            root = Hypothesis(
                id=f"hyp-move-root-{ids['investigation'][-6:]}",
                investigation_id=ids["investigation"],
                statement="根",
                expected_observations=["x"],
            )
            await case.store.save_hypothesis(root)
            child = Hypothesis(
                id=f"hyp-move-child-{ids['investigation'][-6:]}",
                investigation_id=ids["investigation"],
                parent_id=root.id,
                statement="子",
                expected_observations=["y"],
            )
            await case.store.save_hypothesis(child)
            other = Hypothesis(
                id=f"hyp-move-other-{ids['investigation'][-6:]}",
                investigation_id=ids["investigation"],
                statement="新父",
                expected_observations=["z"],
            )
            await case.store.save_hypothesis(other)
            _, context = await case.runtime._open_context()
            fake_runtime = ToolRuntime(
                state={},
                context=context,
                config={},
                stream_writer=None,
                tool_call_id="call-move",
                store=None,
            )
            message = await manage_hypotheses.coroutine(
                operations=[
                    HypothesisOperation(
                        op="refine",
                        hypothesis_id=child.id,
                        parent_id=other.id,
                        reason="改挂到新父节点",
                    )
                ],
                runtime=fake_runtime,
            )
            moved = await case.store.get_hypothesis(child.id)
            revisions = [
                row
                for row in await case.store.list_hypothesis_revisions(ids["investigation"])
                if row["hypothesis_id"] == child.id
            ]
            return message, moved, revisions

    message, moved, revisions = asyncio.run(run())
    assert '"status": "ok"' in message
    assert moved.parent_id is not None
    assert moved.parent_id.startswith("hyp-move-other-")
    assert [row["revision"] for row in revisions] == [1, 2]


def test_cross_investigation_observation_is_rejected(ids):
    """证据判定不能引用别的调查的观测。"""
    from langchain.tools import ToolRuntime

    from aiops.agent.tools import assess_evidence

    async def run():
        async with agent_case(ids, responses=[]) as case:
            hypothesis = Hypothesis(
                id=f"hyp-own-{ids['investigation'][-6:]}",
                investigation_id=ids["investigation"],
                statement="本次调查的假设",
                expected_observations=["x"],
            )
            await case.store.save_hypothesis(hypothesis)
            _, context = await case.runtime._open_context()
            fake_runtime = ToolRuntime(
                state={},
                context=context,
                config={},
                stream_writer=None,
                tool_call_id="call-cross-obs",
                store=None,
            )
            return await assess_evidence.coroutine(
                links=[
                    {
                        "observation_id": "obs-from-another-investigation",
                        "hypothesis_id": hypothesis.id,
                        "relation": "supporting",
                        "rationale": "跨调查引用",
                    }
                ],
                hypothesis_updates=[],
                runtime=fake_runtime,
            )

    message = asyncio.run(run())
    assert "unknown_observation" in message


def test_tools_in_one_model_batch_run_one_at_a_time(ids):
    class SlowToolbox(FakeToolbox):
        def __init__(self) -> None:
            super().__init__()
            self.active = 0
            self.max_active = 0
            self.order: list[str] = []

        async def run(self, context, time_range, request):  # noqa: ANN001
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.order.append(request.tool)
            try:
                await asyncio.sleep(0.05)
                return await super().run(context, time_range, request)
            finally:
                self.active -= 1

    toolbox = SlowToolbox()
    responses = [
        _two_probes(),
        lambda messages: report_call(
            conclusion="两路探测都返回 0",
            confirmed=False,
            boundary="只有探测数据",
        ),
    ]

    async def run():
        async with agent_case(ids, responses=responses, toolbox=toolbox) as case:
            report = await case.runtime.run()
            observations = await case.store.list_observations(ids["investigation"])
            return report, observations

    report, observations = asyncio.run(run())
    assert report.status is InvestigationStatus.INCONCLUSIVE
    assert toolbox.max_active == 1
    assert toolbox.order == ["prometheus.query_range", "prometheus.query_range"]
    assert len(observations) == 2


def _two_probes():
    from langchain_core.messages import AIMessage

    return AIMessage(
        content="",
        tool_calls=[
            {"name": "prometheus_query_range", "args": PROBE, "id": "call-probe-a"},
            {
                "name": "prometheus_query_range",
                "args": {**PROBE, "step_seconds": 30},
                "id": "call-probe-b",
            },
        ],
    )
