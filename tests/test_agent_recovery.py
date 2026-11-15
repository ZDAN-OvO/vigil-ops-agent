"""恢复与故障边界：租约、取消、写意图未知、审批过期与重复执行。"""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from db_env import DSN
from investigation_support import (
    FakeRestartAdapter,
    FakeRolloutAdapter,
    agent_case,
    create_hypothesis,
    make_proposal_executor,
    observation_ids,
    report_call,
)
from langchain.tools import ToolRuntime
from test_agent_actions import LOGS, PODS, _assess, _request

from aiops.agent.engine import LeaseConflict, WriteIntentUnknown
from aiops.agent.tools import request_action
from aiops.contracts import ActionAttempt, AttemptStatus, InvestigationTask, TaskPhase

pytestmark = pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL，跳过真实库测试")


def _preamble():
    return [create_hypothesis(), PODS, LOGS, _assess, _request]


def _valid_report(messages):
    return report_call(
        confirmed=True,
        conclusion="Pod 级状态损坏",
        supporting=observation_ids(messages),
    )


def test_cancel_before_start_aborts_without_model_calls(ids):
    async def run():
        async with agent_case(ids, responses=[]) as case:
            await case.store.create_task(
                InvestigationTask(
                    investigation_id=ids["investigation"],
                    phase=TaskPhase.RUNNING,
                    runtime_version="langchain-agent-1",
                    calendar_started_at=datetime.now(UTC),
                )
            )
            await case.store.request_cancel(ids["investigation"], at=datetime.now(UTC))
            report = await case.runtime.run()
            return report, len(case.model.seen), case

    report, calls, case = asyncio.run(run())
    assert report.task["outcome"] == "aborted"
    assert report.task["termination_code"] == "cancelled"
    assert calls == 0


def test_lease_conflict_blocks_concurrent_driver(ids):
    async def run():
        async with agent_case(ids, responses=[]) as case:
            await case.store.create_task(
                InvestigationTask(
                    investigation_id=ids["investigation"],
                    phase=TaskPhase.RUNNING,
                    runtime_version="langchain-agent-1",
                    calendar_started_at=datetime.now(UTC),
                )
            )
            assert await case.store.acquire_task_lease(
                ids["investigation"], "other-process", seconds=300
            )
            try:
                await case.runtime.run()
                return None
            except LeaseConflict as exc:
                return str(exc)

    message = asyncio.run(run())
    assert message is not None and "驱动者" in message


def test_cancel_while_waiting_marks_aborted_on_resume(ids):
    async def run():
        async with agent_case(
            ids, responses=[*_preamble(), _valid_report], namespace="vigil-shop"
        ) as case:
            case.runtime._executor = make_proposal_executor(
                case.store, restart=FakeRestartAdapter(), rollout=FakeRolloutAdapter()
            )
            first = await case.runtime.run()
            proposals = await case.store.list_proposals_for_investigation(ids["investigation"])
            await case.store.request_cancel(ids["investigation"], at=datetime.now(UTC))
            second = await case.runtime.resume(
                proposal_id=proposals[0].id, decision="approved", actor="operator"
            )
            attempts = await case.store.count_attempts_for_investigation(ids["investigation"])
            return first, second, attempts, case

    first, second, attempts, case = asyncio.run(run())
    assert first.task["phase"] == "waiting_approval"
    assert second.task["outcome"] == "aborted"
    assert second.task["termination_code"] == "cancelled"
    assert attempts == 0


def test_write_intent_blocks_takeover_without_replay(ids):
    async def run():
        async with agent_case(
            ids, responses=[*_preamble(), _valid_report], namespace="vigil-shop"
        ) as case:
            executor = make_proposal_executor(
                case.store, restart=FakeRestartAdapter(), rollout=FakeRolloutAdapter()
            )
            case.runtime._executor = executor
            await case.runtime.run()
            proposals = await case.store.list_proposals_for_investigation(ids["investigation"])
            proposal = proposals[0]
            now = datetime.now(UTC)
            await case.store.save_attempt(
                ActionAttempt(
                    id=f"att-{uuid.uuid4().hex[:8]}",
                    proposal_id=proposal.id,
                    status=AttemptStatus.INTENT,
                    idempotency_key="intent",
                    snapshot={},
                    request={},
                    started_at=now,
                )
            )
            # 模拟驱动进程崩溃：任务回到 running，租约过期。
            async with case.store._pool.connection() as conn:
                await conn.execute(
                    """
                    UPDATE investigation_tasks
                       SET phase = 'running', lease_owner = NULL, lease_expires_at = NULL
                     WHERE investigation_id = %s
                    """,
                    (ids["investigation"],),
                )
            try:
                await case.runtime.resume(
                    proposal_id=proposal.id, decision="approved", actor="operator"
                )
                return None
            except WriteIntentUnknown as exc:
                return str(exc)

    message = asyncio.run(run())
    assert message is not None and "写意图" in message


def test_resume_after_terminal_task_is_refused(ids):
    async def run():
        async with agent_case(
            ids, responses=[report_call(confirmed=False, conclusion="只读结束")]
        ) as case:
            await case.runtime.run()
            try:
                await case.runtime.resume(
                    proposal_id="act-does-not-exist", decision="approved", actor="operator"
                )
                return None
            except Exception as exc:  # noqa: BLE001 - 只关心拒绝类型
                return exc.__class__.__name__, str(exc)

    result = asyncio.run(run())
    assert result is not None
    assert result[0] == "TaskNotResumable"


def test_replayed_action_request_returns_saved_feedback(ids):
    """账本已记录执行结果时，再次进入工具只读回结果，不重复写。"""

    async def run():
        async with agent_case(ids, responses=[*_preamble(), _valid_report]) as case:
            executor = make_proposal_executor(
                case.store, restart=FakeRestartAdapter(), rollout=FakeRolloutAdapter()
            )
            case.runtime._executor = executor
            await case.runtime.run()
            _, context = await case.runtime._open_context()
            hypotheses = await case.store.list_hypotheses(ids["investigation"])
            observations = await case.store.list_observations(ids["investigation"])
            fake_runtime = ToolRuntime(
                state={},
                context=context,
                config={},
                stream_writer=None,
                tool_call_id="call-action",
                store=None,
            )
            replay = await request_action.coroutine(
                fake_runtime,
                action="restart_pod",
                reason="重复请求",
                hypothesis_id=hypotheses[0].id,
                evidence_ids=[item.id for item in observations],
                purpose="diagnostic",
                target_pod="target-pod",
                service="target-service",
                path="/work",
                prediction="重建后返回 200",
                absence_meaning="仍失败说明模板有问题",
                business_target="target-service:/work",
                observation_window_seconds=90,
            )
            attempts = await case.store.count_attempts_for_investigation(ids["investigation"])
            deleted = len(executor._restart_adapter.deleted)
            ledger = await case.store.get_tool_request(ids["investigation"], "call-action")
            return replay, attempts, deleted, ledger

    replay, attempts, deleted, ledger = asyncio.run(run())
    assert attempts == 1
    assert deleted == 1
    assert ledger.state.value == "action_executed"
    assert ledger.action_attempt_id is not None
    assert "proposal_id" in replay
