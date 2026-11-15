"""单 Agent 动作闭环：主动诊断准入、审批中断、执行反馈与恢复后停止。

这些用例使用真实的 ActionExecutor（假适配器），覆盖预算预留、适用性复查、
人工审批恢复和重复执行防护。
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from db_env import DSN
from investigation_support import (
    FakeAgentExecutor,
    FakeRestartAdapter,
    FakeRolloutAdapter,
    ScriptedChatModel,
    agent_case,
    call,
    create_hypothesis,
    hypothesis_id,
    make_proposal_executor,
    observation_ids,
    report_call,
    tool_payloads,
)
from langchain_core.messages import AIMessage

from aiops.actions.outcome import AttemptOutcome
from aiops.agent.engine import LangChainInvestigation, LeaseConflict, TaskNotResumable
from aiops.contracts import (
    Approval,
    ApprovalDecision,
    Budget,
    ProposalStatus,
    RiskDisposition,
    TaskOutcome,
    TerminationCode,
)

pytestmark = pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL，跳过真实库测试")

PODS = call("kubernetes_list_pods", {"selector": "app=target"}, "call-pods")
LOGS = call(
    "kubernetes_list_logs",
    {"selector": "app=target", "pod_name": "target-pod"},
    "call-logs",
)


def _assess(messages):
    hyp = hypothesis_id(messages)
    first, second = observation_ids(messages)[:2]
    return call(
        "assess_evidence",
        {
            "links": [
                {
                    "observation_id": first,
                    "hypothesis_id": hyp,
                    "relation": "supporting",
                    "rationale": "目标 Pod 是唯一副本",
                },
                {
                    "observation_id": second,
                    "hypothesis_id": hyp,
                    "relation": "supporting",
                    "rationale": "Pod 日志持续 500",
                },
            ],
            "hypothesis_updates": [
                {"hypothesis_id": hyp, "status": "supported", "reason": "平台与业务证据一致"}
            ],
        },
        "call-assess",
    )


def _request(
    messages,
    *,
    prediction: str = "重建后新 Pod 的 /work 返回 200",
    absence_meaning: str = "若仍失败则模板或依赖有问题",
    evidence_count: int = 2,
    purpose: str = "diagnostic",
):
    return call(
        "request_action",
        {
            "action": "restart_pod",
            "purpose": purpose,
            "target_pod": "target-pod",
            "service": "target-service",
            "path": "/work",
            "reason": "Pod 级状态异常，重建可以区分模板问题",
            "hypothesis_id": hypothesis_id(messages),
            "evidence_ids": observation_ids(messages)[:evidence_count],
            "prediction": prediction,
            "absence_meaning": absence_meaning,
            "business_target": "target-service:/work",
            "observation_window_seconds": 90,
        },
        "call-action",
    )


def _approved(proposal, *, expires_in: int = 900) -> Approval:
    from aiops.actions.executor import proposal_hash

    now = datetime.now(UTC)
    return Approval(
        id=f"apr-{proposal.id[-6:]}",
        proposal_id=proposal.id,
        proposal_hash=proposal_hash(proposal),
        actor="operator",
        decision=ApprovalDecision.APPROVED,
        reason="人工批准",
        decided_at=now,
        expires_at=now + timedelta(seconds=expires_in),
    )


def test_diagnostic_without_prediction_is_rejected(ids):
    responses = [
        create_hypothesis(),
        PODS,
        LOGS,
        _assess,
        lambda messages: _request(messages, prediction=""),
        report_call(confirmed=False, conclusion="缺少诊断依据，未提出动作"),
    ]

    async def run():
        async with agent_case(
            ids, responses=responses, executor=None, namespace="vigil-experiment"
        ) as case:
            case.runtime._executor = make_proposal_executor(case.store)
            report = await case.runtime.run()
            proposals = await case.store.list_proposals_for_investigation(ids["investigation"])
            return report, proposals, tool_payloads(case.model.seen[-1])

    report, proposals, payloads = asyncio.run(run())
    assert proposals == []
    assert any(payload.get("code") == "diagnostic_incomplete" for payload in payloads)
    assert report.task["outcome"] == TaskOutcome.INCONCLUSIVE.value


def test_sustained_oom_makes_pod_rebuild_inapplicable(ids):
    responses = [
        create_hypothesis(),
        PODS,
        LOGS,
        _assess,
        _request,
        lambda messages: report_call(
            confirmed=True,
            conclusion="持续 OOM，重建不适用",
            supporting=observation_ids(messages),
        ),
    ]

    async def run():
        async with agent_case(ids, responses=responses, namespace="vigil-experiment") as case:
            case.runtime._executor = make_proposal_executor(
                case.store,
                restart=FakeRestartAdapter(
                    last_terminated_reason="OOMKilled", restart_count=2, memory_limit="128Mi"
                ),
                rollout=FakeRolloutAdapter(previous_memory_limit="512Mi"),
            )
            report = await case.runtime.run()
            proposals = await case.store.list_proposals_for_investigation(ids["investigation"])
            return report, proposals, tool_payloads(case.model.seen[-1])

    report, proposals, payloads = asyncio.run(run())
    assert proposals == []
    assert any(payload.get("code") == "oom_template_inapplicable" for payload in payloads)
    assert report.task["outcome"] == TaskOutcome.DIAGNOSIS_ONLY.value


def test_action_requires_supporting_evidence_link(ids):
    responses = [
        create_hypothesis(),
        PODS,
        LOGS,
        _request,
        report_call(confirmed=False, conclusion="证据没有判定，不提出动作"),
    ]

    async def run():
        async with agent_case(ids, responses=responses, namespace="vigil-experiment") as case:
            case.runtime._executor = make_proposal_executor(case.store)
            report = await case.runtime.run()
            proposals = await case.store.list_proposals_for_investigation(ids["investigation"])
            return report, proposals, tool_payloads(case.model.seen[-1])

    report, proposals, payloads = asyncio.run(run())
    assert proposals == []
    assert any("还没有被判定为支持假设" in json_message(payload) for payload in payloads)


def json_message(payload: dict) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)


def test_restart_requires_target_scoped_platform_evidence(ids):
    deployments = call("kubernetes_list_deployments", {"selector": "app=target"}, "call-deploy")
    probe = call(
        "prometheus_query_range",
        {
            "expression": 'probe_success{job="frontend-probe"}',
            "lookback_seconds": 120,
            "step_seconds": 15,
        },
        "call-probe",
    )
    responses = [
        create_hypothesis(),
        deployments,
        probe,
        lambda messages: call(
            "assess_evidence",
            {
                "links": [
                    {
                        "observation_id": observation_ids(messages)[0],
                        "hypothesis_id": hypothesis_id(messages),
                        "relation": "supporting",
                        "rationale": "模板变了",
                    },
                    {
                        "observation_id": observation_ids(messages)[1],
                        "hypothesis_id": hypothesis_id(messages),
                        "relation": "supporting",
                        "rationale": "探测失败",
                    },
                ],
                "hypothesis_updates": [],
            },
            "call-assess",
        ),
        lambda messages: _request(messages),
        report_call(confirmed=False, conclusion="只看到入口现象，重建没有依据"),
    ]

    async def run():
        async with agent_case(ids, responses=responses, namespace="vigil-experiment") as case:
            case.runtime._executor = make_proposal_executor(case.store)
            report = await case.runtime.run()
            proposals = await case.store.list_proposals_for_investigation(ids["investigation"])
            return report, proposals, tool_payloads(case.model.seen[-1])

    report, proposals, payloads = asyncio.run(run())
    assert proposals == []
    assert any("缺少指向目标 Pod 的平台观测" in json_message(item) for item in payloads)


def test_auto_action_executes_once_and_recovers(ids):
    responses = [
        create_hypothesis(),
        PODS,
        LOGS,
        _assess,
        _request,
        lambda messages: report_call(
            confirmed=True,
            conclusion="Pod 级状态损坏",
            supporting=observation_ids(messages),
        ),
    ]

    async def run():
        async with agent_case(ids, responses=responses, namespace="vigil-experiment") as case:
            executor = make_proposal_executor(
                case.store, restart=FakeRestartAdapter(), rollout=FakeRolloutAdapter()
            )
            case.runtime._executor = executor
            report = await case.runtime.run()
            proposals = await case.store.list_proposals_for_investigation(ids["investigation"])
            attempts = await case.store.count_attempts_for_investigation(ids["investigation"])
            task = await case.store.get_task(ids["investigation"])
            return report, proposals, attempts, task, executor, case

    report, proposals, attempts, task, executor, case = asyncio.run(run())
    assert [item.status for item in proposals] == [ProposalStatus.SUCCEEDED]
    assert proposals[0].purpose.value == "diagnostic"
    assert proposals[0].prediction == "重建后新 Pod 的 /work 返回 200"
    assert attempts == 1
    assert task.outcome is TaskOutcome.RECOVERED
    assert report.task["termination_code"] == TerminationCode.BUSINESS_RECOVERED.value
    assert report.prediction["outcome"] == "improved"
    assert report.exit_code() == 0
    assert len(executor._restart_adapter.deleted) == 1


def test_approval_pauses_without_write_then_resume_executes_once(ids):
    responses = [
        create_hypothesis(),
        PODS,
        LOGS,
        _assess,
        _request,
        lambda messages: report_call(
            confirmed=True,
            conclusion="Pod 级状态损坏",
            supporting=observation_ids(messages),
        ),
    ]

    async def run():
        async with agent_case(ids, responses=responses, namespace="vigil-shop") as case:
            executor = make_proposal_executor(
                case.store, restart=FakeRestartAdapter(), rollout=FakeRolloutAdapter()
            )
            case.runtime._executor = executor
            first = await case.runtime.run()
            proposals = await case.store.list_proposals_for_investigation(ids["investigation"])
            attempts_before = await case.store.count_attempts_for_investigation(
                ids["investigation"]
            )
            assert [item.status for item in proposals] == [ProposalStatus.PENDING_APPROVAL]
            await case.store.save_approval(_approved(proposals[0]))
            await case.store.transition_proposal(
                proposals[0].id,
                expected=ProposalStatus.PENDING_APPROVAL,
                target=ProposalStatus.APPROVED,
            )
            case.model.responses = [
                lambda messages: report_call(
                    confirmed=True,
                    conclusion="Pod 级状态损坏",
                    supporting=observation_ids(messages),
                )
            ]
            second = await case.runtime.resume(
                proposal_id=proposals[0].id, decision="approved", actor="operator"
            )
            attempts_after = await case.store.count_attempts_for_investigation(ids["investigation"])
            with_error = None
            try:
                await case.runtime.resume(
                    proposal_id=proposals[0].id, decision="approved", actor="operator"
                )
            except TaskNotResumable as exc:
                with_error = str(exc)
            return first, second, attempts_before, attempts_after, executor, with_error

    first, second, attempts_before, attempts_after, executor, with_error = asyncio.run(run())
    assert first.task["phase"] == "waiting_approval"
    assert first.task["outcome"] is None
    assert first.exit_code() == 3
    assert attempts_before == 0
    assert second.task["outcome"] == TaskOutcome.RECOVERED.value
    assert attempts_after == 1
    assert len(executor._restart_adapter.deleted) == 1
    assert with_error is not None


def test_rejection_returns_to_agent_without_write(ids):
    responses = [
        create_hypothesis(),
        PODS,
        LOGS,
        _assess,
        _request,
        report_call(confirmed=True, conclusion="Pod 级状态损坏，等待人工"),
    ]

    async def run():
        async with agent_case(ids, responses=responses, namespace="vigil-shop") as case:
            executor = make_proposal_executor(
                case.store, restart=FakeRestartAdapter(), rollout=FakeRolloutAdapter()
            )
            case.runtime._executor = executor
            await case.runtime.run()
            proposals = await case.store.list_proposals_for_investigation(ids["investigation"])
            await executor.reject(proposals[0].id, actor="operator", reason="先观察")
            case.model.responses = [
                lambda messages: report_call(
                    confirmed=True,
                    conclusion="Pod 级状态损坏，人工拒绝重建",
                    supporting=observation_ids(messages),
                )
            ]
            report = await case.runtime.resume(
                proposal_id=proposals[0].id, decision="rejected", actor="operator"
            )
            attempts = await case.store.count_attempts_for_investigation(ids["investigation"])
            return report, attempts, executor

    report, attempts, executor = asyncio.run(run())
    assert attempts == 0
    assert executor._restart_adapter.deleted == []
    assert report.task["outcome"] == TaskOutcome.DIAGNOSIS_ONLY.value


def test_expired_approval_does_not_write(ids):
    responses = [
        create_hypothesis(),
        PODS,
        LOGS,
        _assess,
        _request,
        lambda messages: report_call(
            confirmed=True,
            conclusion="Pod 级状态损坏",
            supporting=observation_ids(messages),
        ),
    ]

    async def run():
        async with agent_case(ids, responses=responses, namespace="vigil-shop") as case:
            executor = make_proposal_executor(
                case.store, restart=FakeRestartAdapter(), rollout=FakeRolloutAdapter()
            )
            case.runtime._executor = executor
            await case.runtime.run()
            proposals = await case.store.list_proposals_for_investigation(ids["investigation"])
            await case.store.save_approval(_approved(proposals[0], expires_in=-5))
            await case.store.transition_proposal(
                proposals[0].id,
                expected=ProposalStatus.PENDING_APPROVAL,
                target=ProposalStatus.APPROVED,
            )
            case.model.responses = [
                lambda messages: report_call(
                    confirmed=True,
                    conclusion="审批过期，重建未执行",
                    supporting=observation_ids(messages),
                )
            ]
            report = await case.runtime.resume(
                proposal_id=proposals[0].id, decision="approved", actor="operator"
            )
            attempts = await case.store.count_attempts_for_investigation(ids["investigation"])
            refreshed = await case.store.get_proposal(proposals[0].id)
            return report, attempts, refreshed, executor

    report, attempts, refreshed, executor = asyncio.run(run())
    assert attempts == 0
    assert executor._restart_adapter.deleted == []
    assert refreshed.status is ProposalStatus.EXPIRED
    assert report.task["outcome"] == TaskOutcome.DIAGNOSIS_ONLY.value


def test_mixed_batch_is_not_executed(ids):
    mixed = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "request_action",
                "args": {
                    "action": "restart_pod",
                    "purpose": "diagnostic",
                    "target_pod": "target-pod",
                    "service": "target-service",
                    "reason": "混批测试",
                    "hypothesis_id": "hyp-x",
                    "evidence_ids": ["obs-x"],
                    "prediction": "p",
                    "absence_meaning": "a",
                    "business_target": "b",
                },
                "id": "call-action",
            },
            {
                "name": "prometheus_query_range",
                "args": {
                    "expression": 'probe_success{job="frontend-probe"}',
                    "lookback_seconds": 120,
                    "step_seconds": 15,
                },
                "id": "call-probe",
            },
        ],
    )
    responses = [mixed, report_call(confirmed=False, conclusion="混批被拒绝")]

    async def run():
        async with agent_case(ids, responses=responses, namespace="vigil-experiment") as case:
            case.runtime._executor = make_proposal_executor(case.store)
            report = await case.runtime.run()
            proposals = await case.store.list_proposals_for_investigation(ids["investigation"])
            observations = await case.store.list_observations(ids["investigation"])
            requests = await case.store.list_tool_requests(ids["investigation"])
            return report, proposals, observations, requests, case

    report, proposals, observations, requests, case = asyncio.run(run())
    assert proposals == []
    assert observations == []
    assert requests == []
    messages = tool_payloads(case.model.seen[-1])
    assert any("动作请求必须单独" in json_message(item) for item in messages)
    assert report.task["outcome"] == TaskOutcome.INCONCLUSIVE.value


def test_recovered_task_blocks_further_actions(ids):
    responses = [
        create_hypothesis(),
        PODS,
        LOGS,
        _assess,
        _request,
        _request,
        lambda messages: report_call(
            confirmed=True,
            conclusion="Pod 级状态损坏",
            supporting=observation_ids(messages),
        ),
    ]

    async def run():
        async with agent_case(ids, responses=responses, namespace="vigil-experiment") as case:
            executor = make_proposal_executor(
                case.store, restart=FakeRestartAdapter(), rollout=FakeRolloutAdapter()
            )
            case.runtime._executor = executor
            report = await case.runtime.run()
            proposals = await case.store.list_proposals_for_investigation(ids["investigation"])
            attempts = await case.store.count_attempts_for_investigation(ids["investigation"])
            return report, proposals, attempts, executor, case

    report, proposals, attempts, executor, case = asyncio.run(run())
    assert len(proposals) == 1
    assert attempts == 1
    assert len(executor._restart_adapter.deleted) == 1
    assert any("业务已恢复" in str(message.content) for message in case.model.seen[-1])
    assert report.task["outcome"] == TaskOutcome.RECOVERED.value


def test_write_reserve_blocks_action_without_write(ids):
    budget = Budget(
        max_tool_calls=10,
        max_model_calls=8,
        max_wall_clock_seconds=120,
        max_action_seconds=10,
    )
    responses = [
        create_hypothesis(),
        PODS,
        LOGS,
        _assess,
        _request,
        report_call(confirmed=False, conclusion="动作预算不足，未写"),
    ]

    async def run():
        async with agent_case(
            ids, responses=responses, namespace="vigil-experiment", budget=budget
        ) as case:
            executor = make_proposal_executor(
                case.store, restart=FakeRestartAdapter(), rollout=FakeRolloutAdapter()
            )
            case.runtime._executor = executor
            report = await case.runtime.run()
            attempts = await case.store.count_attempts_for_investigation(ids["investigation"])
            payloads = tool_payloads(case.model.seen[-1])
            return report, attempts, executor, payloads

    report, attempts, executor, payloads = asyncio.run(run())
    assert attempts == 0
    assert executor._restart_adapter.deleted == []
    assert any(item.get("code") == "budget_reserve" for item in payloads)
    assert report.task["outcome"] == TaskOutcome.INCONCLUSIVE.value


def test_budget_exhaustion_after_recovery_keeps_recovered_outcome(ids):
    responses = [
        create_hypothesis(),
        PODS,
        LOGS,
        _assess,
        _request,
        PODS,
    ]

    async def run():
        async with agent_case(
            ids,
            responses=responses,
            namespace="vigil-experiment",
            budget=Budget(
                max_tool_calls=20,
                max_model_calls=6,
                max_wall_clock_seconds=600,
                max_action_seconds=450,
            ),
        ) as case:
            executor = make_proposal_executor(
                case.store, restart=FakeRestartAdapter(), rollout=FakeRolloutAdapter()
            )
            case.runtime._executor = executor
            report = await case.runtime.run()
            task = await case.store.get_task(ids["investigation"])
            return report, task, executor

    report, task, executor = asyncio.run(run())
    assert task.outcome is TaskOutcome.RECOVERED
    assert task.termination_code == TerminationCode.BUSINESS_RECOVERED.value
    assert report.status.value == "inconclusive"
    assert report.task["termination_code"] == "business_recovered"
    assert report.model_summary_completed is False
    assert report.exit_code() == 0
    assert len(executor._restart_adapter.deleted) == 1


def test_approval_is_not_created_without_feedback_model_budget(ids):
    responses = [create_hypothesis(), PODS, LOGS, _assess, _request]

    async def run():
        async with agent_case(
            ids,
            responses=responses,
            namespace="vigil-shop",
            budget=Budget(
                max_tool_calls=10,
                max_model_calls=5,
                max_wall_clock_seconds=120,
                max_action_seconds=450,
            ),
        ) as case:
            executor = make_proposal_executor(
                case.store, restart=FakeRestartAdapter(), rollout=FakeRolloutAdapter()
            )
            case.runtime._executor = executor
            report = await case.runtime.run()
            proposals = await case.store.list_proposals_for_investigation(ids["investigation"])
            attempts = await case.store.count_attempts_for_investigation(ids["investigation"])
            request = await case.store.get_tool_request(ids["investigation"], "call-action")
            task = await case.store.get_task(ids["investigation"])
            return report, proposals, attempts, request, task

    report, proposals, attempts, request, task = asyncio.run(run())
    assert [item.status for item in proposals] == [ProposalStatus.REJECTED]
    assert attempts == 0
    assert request is not None and request.error_code == "budget_reserve"
    assert task.phase.value == "terminal"
    assert task.termination_code == "budget_exhausted"
    assert report.proposal_id == proposals[0].id


def test_agent_turns_failed_action_into_needs_human(ids):
    responses = [
        create_hypothesis(),
        PODS,
        LOGS,
        _assess,
        _request,
        lambda messages: report_call(
            confirmed=True,
            conclusion="Pod 级状态损坏，但重建未改善业务",
            supporting=observation_ids(messages),
        ),
    ]

    async def run():
        async with agent_case(ids, responses=responses, namespace="vigil-experiment") as case:
            executor = FakeAgentExecutor(
                case.store,
                disposition=RiskDisposition.AUTO,
                outcome=AttemptOutcome.NO_IMPROVEMENT,
            )
            case.runtime._executor = executor
            report = await case.runtime.run()
            task = await case.store.get_task(ids["investigation"])
            return report, task, executor

    report, task, executor = asyncio.run(run())
    assert report.task["outcome"] == TaskOutcome.NEEDS_HUMAN.value
    assert report.task["termination_code"] == TerminationCode.ACTION_NO_IMPROVEMENT.value
    assert report.exit_code() == 3
    assert task.outcome is TaskOutcome.NEEDS_HUMAN
    assert len(executor.executed) == 1


def test_two_concurrent_resumes_write_once(ids):
    responses = [
        create_hypothesis(),
        PODS,
        LOGS,
        _assess,
        _request,
        lambda messages: report_call(
            confirmed=True,
            conclusion="Pod 级状态损坏",
            supporting=observation_ids(messages),
        ),
    ]

    async def run():
        async with agent_case(ids, responses=responses, namespace="vigil-shop") as case:
            executor = make_proposal_executor(
                case.store, restart=FakeRestartAdapter(), rollout=FakeRolloutAdapter()
            )
            case.runtime._executor = executor
            first = await case.runtime.run()
            proposals = await case.store.list_proposals_for_investigation(ids["investigation"])
            await case.store.save_approval(_approved(proposals[0]))
            await case.store.transition_proposal(
                proposals[0].id,
                expected=ProposalStatus.PENDING_APPROVAL,
                target=ProposalStatus.APPROVED,
            )
            report_response = lambda messages: report_call(  # noqa: E731
                confirmed=True,
                conclusion="Pod 级状态损坏",
                supporting=observation_ids(messages),
            )
            case.model.responses = [report_response]
            second_runtime = LangChainInvestigation(
                settings=case.runtime._settings,
                store=case.store,
                dsn=case.runtime._dsn,
                incident=case.incident,
                investigation=case.investigation,
                toolbox=case.toolbox,
                executor=executor,
                model=ScriptedChatModel(responses=[report_response]),  # type: ignore[arg-type]
            )
            results = await asyncio.gather(
                case.runtime.resume(
                    proposal_id=proposals[0].id, decision="approved", actor="operator-a"
                ),
                second_runtime.resume(
                    proposal_id=proposals[0].id, decision="approved", actor="operator-b"
                ),
                return_exceptions=True,
            )
            attempts = await case.store.count_attempts_for_investigation(ids["investigation"])
            return first, results, attempts, executor

    first, results, attempts, executor = asyncio.run(run())
    assert first.task["phase"] == "waiting_approval"
    assert attempts == 1
    assert len(executor._restart_adapter.deleted) == 1
    failures = [item for item in results if isinstance(item, BaseException)]
    assert len(failures) == 1
    assert isinstance(failures[0], (LeaseConflict, TaskNotResumable))
