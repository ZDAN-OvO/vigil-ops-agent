"""Skill 记录、委派调查与新增终止条件的运行时测试。"""

import asyncio
from pathlib import Path

import pytest
from db_env import DSN
from investigation_support import (
    agent_case,
    call,
    create_hypothesis,
    hypothesis_id,
    observation_ids,
    report_call,
)
from langchain_core.messages import AIMessage

from aiops.agent.delegation import ReadOnlyGuardMiddleware, ReadOnlyViolation
from aiops.agent.tools import _find_repeated_request, _query
from aiops.contracts import (
    InvestigationStatus,
    TerminationCode,
    ToolRequest,
    ToolRequestState,
)
from aiops.tools.registry import build_registry, read_only_names, select_tools, tool_name

pytestmark = pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL，跳过真实库测试")


def test_signal_skill_is_recorded_with_hash(ids):
    responses = [
        create_hypothesis(),
        report_call(confirmed=False, conclusion="证据不足"),
    ]

    async def run():
        async with agent_case(ids, responses=responses) as case:
            report = await case.runtime.run()
            saved = await case.store.get_investigation(ids["investigation"])
            return report, saved

    report, saved = asyncio.run(run())
    assert report.status is InvestigationStatus.INCONCLUSIVE
    assert [item.name for item in saved.skills] == ["hypothesis-driven-investigation"]
    assert saved.skills[0].trigger.value == "signal"
    assert len(saved.skills[0].content_hash) == 64


def test_missing_skill_dir_aborts_with_skill_load_failed(ids):
    responses = [report_call(confirmed=False, conclusion="不会执行到这里")]

    async def run():
        async with agent_case(ids, responses=responses) as case:
            case.runtime._settings = case.runtime._settings.model_copy(
                update={"skills_dir": Path("missing-skills-dir")}
            )
            return await case.runtime.run()

    report = asyncio.run(run())
    assert report.status is InvestigationStatus.ABORTED
    assert report.task is not None
    assert report.task["termination_code"] == TerminationCode.SKILL_LOAD_FAILED.value


def test_delegation_is_read_only_and_attributes_observations(ids):
    def task_call():
        return call(
            "task",
            {
                "description": "核对最近的发布是否变化，返回证据。",
                "subagent_type": "general-purpose",
            },
            "call-task",
        )

    responses = [
        create_hypothesis(),
        task_call(),
        call(
            "prometheus_query_range",
            {
                "expression": 'probe_success{job="frontend-probe"}',
                "lookback_seconds": 120,
                "step_seconds": 15,
            },
            "call-sub-probe",
        ),
        AIMessage(content="子调查：probe 返回 0，说明入口失败。"),
        lambda messages: report_call(
            confirmed=False,
            conclusion="证据不足",
            candidates=[hypothesis_id(messages)],
            supporting=observation_ids(messages),
        ),
    ]

    async def run():
        async with agent_case(ids, responses=responses) as case:
            report = await case.runtime.run()
            delegations = await case.store.list_delegations(ids["investigation"])
            observations = await case.store.list_observations(ids["investigation"])
            task = await case.store.get_task(ids["investigation"])
            return report, delegations, observations, task

    report, delegations, observations, task = asyncio.run(run())
    assert report.status is InvestigationStatus.INCONCLUSIVE
    assert len(delegations) == 1
    assert delegations[0].status.value == "succeeded"
    assert "发布" in delegations[0].objective
    assert delegations[0].result_summary.startswith("子调查")
    assert "hypothesis-driven-investigation" in delegations[0].skills
    assert observations[0].delegation_id == delegations[0].id
    assert report.delegations[0]["id"] == delegations[0].id
    assert report.delegations[0]["observation_ids"] == [
        item.id for item in observations if item.delegation_id == delegations[0].id
    ]
    assert task is not None and task.used_tool_calls >= 2
    assert task.used_model_calls >= 4


def test_read_only_guard_rejects_write_tool():
    allowed = read_only_names(build_registry())
    middleware = ReadOnlyGuardMiddleware(allowed)

    class Request:
        tool_call = {"name": "request_action", "id": "call-1"}

    async def handler(_request):
        return "should not run"

    async def run():
        await middleware.awrap_tool_call(Request(), handler)

    with pytest.raises(ReadOnlyViolation):
        asyncio.run(run())


def test_general_purpose_subagent_only_gets_read_only_tools():
    registry = build_registry()
    names = {tool_name(item) for item in select_tools(registry, read_only=True)}
    assert all(spec.input_model for spec in registry)
    assert all(spec.output_model == "str" for spec in registry)
    assert names == read_only_names(registry)
    assert "request_action" not in names
    assert "manage_hypotheses" not in names
    assert "prometheus_query_range" in names


def test_repeated_read_is_not_new_progress(ids):
    async def run():
        async with agent_case(ids, responses=[]) as case:
            _, context = await case.runtime._open_context()
            request = _query(
                "prometheus.query_range",
                expression="probe_success",
                lookback_seconds=60,
                step_seconds=15,
            )
            await case.store.start_tool_request(
                ToolRequest(
                    investigation_id=ids["investigation"],
                    tool_call_id="call-first",
                    tool="prometheus_query_range",
                    arguments=request.model_dump(),
                    state=ToolRequestState.OBSERVED,
                    started_at=case.runtime._investigation.started_at,
                )
            )
            found = await _find_repeated_request(
                context, "prometheus_query_range", request, "call-second"
            )
            return found is not None

    assert asyncio.run(run()) is True


def test_repeated_read_reuses_saved_observation(ids):
    probe = {
        "expression": 'probe_success{job="frontend-probe"}',
        "lookback_seconds": 120,
        "step_seconds": 15,
    }
    responses = [
        create_hypothesis(),
        call("prometheus_query_range", probe, "call-probe-1"),
        call("prometheus_query_range", probe, "call-probe-2"),
        lambda messages: report_call(
            confirmed=False,
            conclusion="重复读取不产生新观测",
            candidates=[hypothesis_id(messages)],
            supporting=observation_ids(messages),
        ),
    ]

    async def run():
        async with agent_case(ids, responses=responses) as case:
            report = await case.runtime.run()
            observations = await case.store.list_observations(ids["investigation"])
            task = await case.store.get_task(ids["investigation"])
            return report, observations, task

    report, observations, task = asyncio.run(run())
    assert report.status is InvestigationStatus.INCONCLUSIVE
    assert len(observations) == 1
    assert task is not None and task.used_tool_calls == 2


def test_two_ungrounded_actions_abort(ids):
    def action(call_id: str):
        return call(
            "request_action",
            {
                "action": "restart_pod",
                "reason": "测试",
                "hypothesis_id": "hyp-missing",
                "evidence_ids": ["obs-missing"],
                "target_pod": "target-pod",
                "service": "target-service",
                "path": "/work",
            },
            call_id,
        )

    responses = [
        create_hypothesis(),
        action("call-action-1"),
        action("call-action-2"),
    ]

    async def run():
        async with agent_case(ids, responses=responses) as case:
            return await case.runtime.run()

    report = asyncio.run(run())
    assert report.status is InvestigationStatus.ABORTED
    assert report.task is not None
    assert report.task["termination_code"] == TerminationCode.UNGROUNDED_ACTION.value


def test_read_only_violation_aborts_and_audits(ids):
    responses = [report_call(confirmed=False, conclusion="不会执行到这里")]

    async def run():
        async with agent_case(ids, responses=responses) as case:

            def boom(_catalog):
                raise ReadOnlyViolation("子 Agent 拿到了写工具")

            case.runtime._build_subagent_app = boom
            report = await case.runtime.run()
            audit = await case.store.list_audit(ids["investigation"])
            return report, audit

    report, audit = asyncio.run(run())
    assert report.status is InvestigationStatus.ABORTED
    assert report.task is not None
    assert report.task["termination_code"] == TerminationCode.READ_ONLY_VIOLATION.value
    assert any(item.event == "delegation.read_only_violation" for item in audit)
