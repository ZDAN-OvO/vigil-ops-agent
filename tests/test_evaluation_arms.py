"""对照臂的最小装配差异：generic 只去掉 Skill 与排查方法，不动工具与治理。"""

import asyncio

import pytest
from db_env import DSN
from investigation_support import (
    agent_case,
    call,
    create_hypothesis,
    observation_ids,
    report_call,
)

from aiops.agent.prompts import GENERIC_SYSTEM_PROMPT, SYSTEM_PROMPT
from aiops.contracts import GENERIC_RUNTIME_VERSION

pytestmark = pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL，跳过真实库测试")


async def _run(case):
    return await case.runtime.run()


def test_generic_arm_uses_a_neutral_prompt_and_generic_runtime(ids):
    async def scenario():
        responses = [
            create_hypothesis(),
            call(
                "prometheus_query_range",
                {
                    "expression": 'probe_success{job="frontend-probe"}',
                    "lookback_seconds": 120,
                    "step_seconds": 15,
                },
                "call-probe",
            ),
            lambda messages: report_call(
                conclusion="入口成功率归零",
                supporting=observation_ids(messages),
            ),
        ]
        async with agent_case(ids, responses=responses, arm="generic") as case:
            report = await _run(case)
            task = await case.store.get_task(case.investigation.id)
            detail = await case.store.get_investigation(case.investigation.id)
            return case, report, task, detail

    case, report, task, detail = asyncio.run(scenario())
    assert case.runtime._system_prompt == GENERIC_SYSTEM_PROMPT
    assert case.runtime._system_prompt != SYSTEM_PROMPT
    assert task is not None and task.runtime_version == GENERIC_RUNTIME_VERSION
    assert detail is not None and detail.skills == []
    assert report.investigation_id == case.investigation.id


def test_agent_arm_keeps_the_methodology_prompt(ids):
    async def scenario():
        responses = [
            create_hypothesis(),
            lambda messages: report_call(
                conclusion="入口成功率归零",
                supporting=observation_ids(messages),
            ),
        ]
        async with agent_case(ids, responses=responses) as case:
            await case.runtime.run()
            task = await case.store.get_task(case.investigation.id)
            return case, task

    case, task = asyncio.run(scenario())
    assert case.runtime._system_prompt == SYSTEM_PROMPT
    assert task is not None and task.runtime_version != GENERIC_RUNTIME_VERSION
