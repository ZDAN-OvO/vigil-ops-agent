"""用真实主线与动作事务验证诊断准入、审批、发送、反馈和预算。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from db_env import DSN
from investigation_support import (
    FakeRestartAdapter,
    FakeRolloutAdapter,
    action_case,
    call,
    create_hypothesis,
    hypothesis_id,
    observation_ids,
    report_call,
    tool_payloads,
)
from langchain_core.messages import AIMessage, ToolMessage

from aiops.actions.rollout_undo import ActionError
from aiops.actions.safety import ActionSafety
from aiops.contracts import (
    Approval,
    ApprovalDecision,
    AttemptStatus,
    InvestigationStatus,
    ProposalStatus,
    TaskOutcome,
    TaskPhase,
    TerminationCode,
    ToolRequestState,
)
from aiops.db import Store

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
    call_id: str = "call-action",
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
        call_id,
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


def _preamble():
    return [create_hypothesis(), PODS, LOGS, _assess, _request]


def _valid_report(messages):
    return report_call(
        confirmed=True,
        conclusion="Pod 级状态损坏",
        supporting=observation_ids(messages),
    )


async def _drive(case):
    async with case.driver.claim(case.session_id) as main:
        assert main is not None
        return await main.drive()


def _investigation_id(result):
    return result.result.payload["investigation_id"]


def _action_message(messages, code):
    for payload in tool_payloads(messages):
        feedback = payload.get("error") or payload
        if feedback.get("code") == code:
            return feedback
    pytest.fail(f"未收到动作反馈：{code}")


async def _assert_blocked(case, code, *, call_id="call-action"):
    done = await _drive(case)
    investigation_id = _investigation_id(done)
    task = await case.store.get_task(investigation_id)
    request = await case.store.get_tool_request(investigation_id, call_id)
    assert done.card is None
    assert request.state is ToolRequestState.REJECTED
    assert request.error_code == code
    _action_message(case.model.seen[-1], code)
    assert await case.store.list_proposals_for_investigation(investigation_id) == []
    assert await case.events("approval_required") == []
    assert await case.events("action_result") == []
    assert case.restart.deleted == []
    return done, task


async def _paused(case):
    pause = await _drive(case)
    assert pause.exit_code == 3
    assert pause.result.payload["status"] == "awaiting_approval"
    card = pause.card
    assert card.type == "approval_required"
    assert card.turn_id == pause.result.turn_id
    assert card.session_id == case.session_id
    assert card.payload["risk"]["disposition"] == "approval"
    assert card.payload["status"] == ProposalStatus.PENDING_APPROVAL
    assert card.payload["action"] == "restart_pod"
    assert card.payload["target"]["name"] == "target-pod"
    proposal_id = card.payload["card_id"]
    assert pause.result.payload["card_id"] == proposal_id
    investigation_id = _investigation_id(pause)
    task = await case.store.get_task(investigation_id)
    assert task.phase is TaskPhase.WAITING_APPROVAL
    assert task.outcome is None
    request = await case.store.get_tool_request(investigation_id, "call-action")
    assert request.state is ToolRequestState.ACTION_PENDING
    assert request.proposal_id == proposal_id
    proposal = await case.store.get_proposal(proposal_id)
    assert proposal.session_id == case.session_id
    assert proposal.session_turn_id == card.turn_id
    assert proposal.investigation_id == investigation_id
    assert proposal.status is ProposalStatus.PENDING_APPROVAL
    assert await case.safety.get_card(proposal_id, session_id=case.session_id) == card.payload
    assert await case.events("approval_required") == [card]
    assert await case.events("card_decision") == []
    assert await case.events("action_result") == []
    assert await case.store.get_attempt(proposal_id) is None
    assert case.restart.dry_runs == []
    assert case.restart.deleted == []
    return pause, proposal


async def _decide(safety, card, *, decision=ApprovalDecision.APPROVED, actor="operator"):
    return await safety.record_card_decision(
        card.payload["card_id"],
        session_id=card.session_id,
        fingerprint=card.payload["fingerprint"],
        decision=decision,
        actor=actor,
        reason="已核对平台和业务观测",
    )


async def _assert_sent(case, done, *, attempt_status=AttemptStatus.VERIFIED):
    investigation_id = _investigation_id(done)
    (proposal,) = await case.store.list_proposals_for_investigation(investigation_id)
    attempt = await case.store.get_attempt(proposal.id)
    assert attempt.status is attempt_status
    assert attempt.send_granted_at is not None
    assert attempt.snapshot["uid"] == proposal.target.uid
    assert case.restart.dry_runs == [proposal.target.uid]
    assert case.restart.deleted == [proposal.target.uid]
    assert await case.store.count_attempts_for_investigation(investigation_id) == 1
    request = await case.store.get_tool_request(investigation_id, "call-action")
    assert request.state is ToolRequestState.ACTION_EXECUTED
    assert request.proposal_id == proposal.id
    assert request.action_attempt_id == attempt.id
    feedback = json.loads(request.result["tool_message"])
    assert feedback["proposal_id"] == proposal.id
    assert feedback["attempt_id"] == attempt.id
    events = await case.events("action_result")
    assert [event.payload["status"] for event in events] == [
        AttemptStatus.INTENT,
        AttemptStatus.EXECUTING,
        attempt_status,
    ]
    assert [event.payload["written"] for event in events] == [False, None, True]
    for event in events:
        assert event.turn_id == done.result.turn_id
        assert event.payload["card_id"] == proposal.id
        assert event.payload["attempt_id"] == attempt.id
    assert events[-1].payload["verification"]["outcome"] == attempt.verification["outcome"]
    assert events[-1].seq < done.result.seq
    grants = [
        event
        for event in await case.store.list_audit(proposal.id)
        if event.event == "action.send_granted"
    ]
    assert [event.payload["attempt_id"] for event in grants] == [attempt.id]
    assert feedback["write_sent"] is events[-1].payload["written"]
    return proposal, attempt


def test_diagnostic_without_prediction_is_rejected(tmp_path, action_session_ids):
    responses = [
        create_hypothesis(),
        PODS,
        LOGS,
        _assess,
        lambda messages: _request(messages, prediction=""),
        report_call(confirmed=False, conclusion="缺少诊断依据，未提出动作"),
    ]

    async def run():
        async with action_case(tmp_path, action_session_ids, responses=responses) as case:
            done, task = await _assert_blocked(case, "diagnostic_incomplete")
            assert task.outcome is TaskOutcome.INCONCLUSIVE
            assert done.result.payload["termination_code"] == TerminationCode.INSUFFICIENT_EVIDENCE

    asyncio.run(run())


def test_sustained_oom_makes_pod_rebuild_inapplicable(tmp_path, action_session_ids):
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
        async with action_case(
            tmp_path,
            action_session_ids,
            responses=responses,
            restart=FakeRestartAdapter(
                last_terminated_reason="OOMKilled", restart_count=2, memory_limit="128Mi"
            ),
            rollout=FakeRolloutAdapter(previous_memory_limit="512Mi"),
        ) as case:
            _, task = await _assert_blocked(case, "oom_template_inapplicable")
            assert task.outcome is TaskOutcome.DIAGNOSIS_ONLY

    asyncio.run(run())


def test_action_requires_supporting_evidence_link(tmp_path, action_session_ids):
    responses = [
        create_hypothesis(),
        PODS,
        LOGS,
        _request,
        report_call(confirmed=False, conclusion="证据没有判定，不提出动作"),
    ]

    async def run():
        async with action_case(tmp_path, action_session_ids, responses=responses) as case:
            await _assert_blocked(case, "invalid_action_request")
            message = _action_message(case.model.seen[-1], "invalid_action_request")
            assert "还没有被判定为支持假设" in message["message"]
            assert "当前支持该假设的观测：无" in message["message"]

    asyncio.run(run())


def test_ungrounded_action_lists_usable_supporting_observations(tmp_path, action_session_ids):
    def assess_one(messages):
        hyp = hypothesis_id(messages)
        first = observation_ids(messages)[0]
        return call(
            "assess_evidence",
            {
                "links": [
                    {
                        "observation_id": first,
                        "hypothesis_id": hyp,
                        "relation": "supporting",
                        "rationale": "目标 Pod 平台状态异常",
                    }
                ],
                "hypothesis_updates": [
                    {"hypothesis_id": hyp, "status": "supported", "reason": "平台证据"}
                ],
            },
            "call-assess-one",
        )

    def request_other(messages):
        hyp = hypothesis_id(messages)
        other = observation_ids(messages)[1]
        return call(
            "request_action",
            {
                "action": "restart_pod",
                "purpose": "diagnostic",
                "target_pod": "target-pod",
                "service": "target-service",
                "path": "/work",
                "reason": "Pod 级状态异常，重建可以区分模板问题",
                "hypothesis_id": hyp,
                "evidence_ids": [other],
                "prediction": "重建后新 Pod 的 /work 返回 200",
                "absence_meaning": "若仍失败则模板或依赖有问题",
                "business_target": "target-service:/work",
                "observation_window_seconds": 90,
            },
            "call-action-other",
        )

    responses = [
        create_hypothesis(),
        PODS,
        LOGS,
        assess_one,
        request_other,
        report_call(confirmed=False, conclusion="证据不足"),
    ]

    async def run():
        async with action_case(tmp_path, action_session_ids, responses=responses) as case:
            done, _ = await _assert_blocked(
                case, "invalid_action_request", call_id="call-action-other"
            )
            observations = await case.store.list_observations(_investigation_id(done))
            message = _action_message(case.model.seen[-1], "invalid_action_request")["message"]
            assert "还没有被判定为支持假设" in message
            assert "当前支持该假设的观测" in message
            assert observations[0].id in message

    asyncio.run(run())


def test_restart_requires_target_scoped_platform_evidence(tmp_path, action_session_ids):
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
        async with action_case(tmp_path, action_session_ids, responses=responses) as case:
            await _assert_blocked(case, "invalid_action_request")
            message = _action_message(case.model.seen[-1], "invalid_action_request")
            assert "缺少指向目标 Pod 的平台观测" in message["message"]

    asyncio.run(run())


def test_auto_action_executes_once_and_recovers(tmp_path, action_session_ids):
    async def run():
        async with action_case(
            tmp_path, action_session_ids, responses=[*_preamble(), _valid_report]
        ) as case:
            done = await _drive(case)
            proposal, attempt = await _assert_sent(case, done)
            task = await case.store.get_task(_investigation_id(done))
            assert proposal.status is ProposalStatus.SUCCEEDED
            assert proposal.purpose.value == "diagnostic"
            assert proposal.prediction == "重建后新 Pod 的 /work 返回 200"
            assert attempt.verification["outcome"] == "improved"
            assert task.outcome is TaskOutcome.RECOVERED
            assert done.result.payload["termination_code"] == TerminationCode.BUSINESS_RECOVERED
            assert done.exit_code == 0
            assert done.card is None
            assert await case.events("approval_required") == []
            assert await case.events("card_decision") == []
            approval = await case.store.get_approval(proposal.id)
            assert approval.actor.startswith("policy:")
            assert approval.proposal_hash == attempt.snapshot["approval_hash"]

    asyncio.run(run())


def test_approval_pauses_without_write_then_resume_executes_once(tmp_path, action_session_ids):
    async def run():
        async with action_case(
            tmp_path,
            action_session_ids,
            responses=[*_preamble(), _valid_report],
            namespace="vigil-shop",
        ) as case:
            pause, proposal = await _paused(case)
            assert (await _drive(case)).card == pause.card
            async with Store.open(DSN) as operator:
                approval = await _decide(ActionSafety(operator), pause.card)
                assert await operator.get_approval(proposal.id) == approval
                assert (await operator.get_proposal(proposal.id)).status is ProposalStatus.APPROVED
            (decision,) = await case.events("card_decision")
            assert pause.card.seq < decision.seq
            assert decision.turn_id == pause.card.turn_id
            assert decision.payload["card_id"] == proposal.id
            assert decision.payload["status"] == "approved"
            assert decision.payload["decision_id"] == approval.id
            assert approval.proposal_hash == pause.card.payload["fingerprint"]
            done = await _drive(case)
            assert done.result.turn_id == pause.result.turn_id
            assert (await case.store.get_task(_investigation_id(done))).outcome is (
                TaskOutcome.RECOVERED
            )
            assert await _drive(case) is None
            with pytest.raises(ActionError) as caught:
                await _decide(case.safety, pause.card)
            assert caught.value.code == "not_pending"
            assert await case.events("approval_required") == [pause.card]
            assert await case.events("card_decision") == [decision]
            _, attempt = await _assert_sent(case, done)
            assert attempt.snapshot["approval_hash"] == approval.proposal_hash
            assert decision.seq < (await case.events("action_result"))[0].seq

    asyncio.run(run())


def test_rejection_returns_to_agent_without_write(tmp_path, action_session_ids):
    async def run():
        async with action_case(
            tmp_path,
            action_session_ids,
            responses=[*_preamble(), _valid_report],
            namespace="vigil-shop",
        ) as case:
            pause, proposal = await _paused(case)
            rejected = await _decide(case.safety, pause.card, decision=ApprovalDecision.REJECTED)
            assert rejected.expires_at is None
            done = await _drive(case)
            assert done.exit_code == 0
            assert done.result.turn_id == pause.card.turn_id
            assert (await case.store.get_task(_investigation_id(done))).outcome is (
                TaskOutcome.DIAGNOSIS_ONLY
            )
            assert (await case.store.get_proposal(proposal.id)).status is ProposalStatus.REJECTED
            assert await case.store.get_attempt(proposal.id) is None
            assert case.restart.deleted == []
            assert await case.events("action_result") == []
            (decision,) = await case.events("card_decision")
            assert pause.card.seq < decision.seq < done.result.seq
            assert decision.turn_id == pause.card.turn_id
            assert decision.payload["card_id"] == proposal.id
            assert decision.payload["status"] == "rejected"
            assert decision.payload["decision_id"] == rejected.id
            request = await case.store.get_tool_request(_investigation_id(done), "call-action")
            assert request.state is ToolRequestState.REJECTED
            feedback = json.loads(request.result["tool_message"])
            assert feedback["status"] == "rejected"
            assert feedback["write_sent"] is False
            assert feedback["reason"] == rejected.reason

    asyncio.run(run())


def test_expired_approval_does_not_write(tmp_path, action_session_ids):
    async def run():
        async with action_case(
            tmp_path,
            action_session_ids,
            responses=[*_preamble(), _valid_report],
            namespace="vigil-shop",
        ) as case:
            pause, proposal = await _paused(case)
            approval = await _decide(case.safety, pause.card)
            # 缩短持久化授权的时限，按数据库时间过期，避免等待实际时钟。
            async with case.store.transaction() as conn:
                await conn.execute(
                    "UPDATE approvals SET expires_at = decided_at WHERE id = %s",
                    (approval.id,),
                )
            done = await _drive(case)
            assert done.exit_code == 0
            assert (await case.store.get_proposal(proposal.id)).status is ProposalStatus.EXPIRED
            assert (await case.store.get_approval(proposal.id)).revoked_at is not None
            assert await case.store.get_attempt(proposal.id) is None
            assert case.restart.deleted == []
            assert (await case.store.get_task(_investigation_id(done))).outcome is (
                TaskOutcome.DIAGNOSIS_ONLY
            )
            (decision,) = await case.events("card_decision")
            (event,) = await case.events("action_result")
            assert decision.payload["decision_id"] == approval.id
            assert decision.seq < event.seq < done.result.seq
            assert event.turn_id == pause.card.turn_id
            assert event.payload["card_id"] == proposal.id
            assert event.payload["proposal_status"] == "expired"
            assert event.payload["attempt_id"] is None
            assert event.payload["written"] is False
            feedback = _action_message(case.model.seen[-1], "expired")
            assert feedback["write_sent"] is False

    asyncio.run(run())


def test_mixed_batch_is_not_executed(tmp_path, action_session_ids):
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
        async with action_case(tmp_path, action_session_ids, responses=responses) as case:
            done = await _drive(case)
            investigation_id = _investigation_id(done)
            assert await case.store.list_proposals_for_investigation(investigation_id) == []
            assert await case.store.list_observations(investigation_id) == []
            assert await case.store.list_tool_requests(investigation_id) == []
            assert case.toolbox.requests == []
            assert case.restart.deleted == []
            assert await case.events("action_result") == []
            assert any(
                "动作请求必须单独" in str(payload) for payload in tool_payloads(case.model.seen[-1])
            )
            assert (await case.store.get_task(investigation_id)).outcome is TaskOutcome.INCONCLUSIVE

    asyncio.run(run())


def test_recovered_task_blocks_further_actions(tmp_path, action_session_ids):
    async def run():
        async with action_case(
            tmp_path,
            action_session_ids,
            responses=[
                *_preamble(),
                lambda messages: _request(messages, call_id="call-second-action"),
                _valid_report,
            ],
        ) as case:
            done = await _drive(case)
            await _assert_sent(case, done)
            investigation_id = _investigation_id(done)
            assert await case.store.get_tool_request(investigation_id, "call-second-action") is None
            (feedback,) = [
                message
                for message in case.model.seen[-1]
                if isinstance(message, ToolMessage) and message.tool_call_id == "call-second-action"
            ]
            assert feedback.status == "error"
            assert "业务已恢复" in feedback.content
            assert (await case.store.get_task(investigation_id)).outcome is TaskOutcome.RECOVERED

    asyncio.run(run())


async def _assert_budget_voided(case, done):
    investigation_id = _investigation_id(done)
    (proposal,) = await case.store.list_proposals_for_investigation(investigation_id)
    assert proposal.status is ProposalStatus.VOIDED
    assert done.card is None
    assert await case.events("approval_required") == []
    assert await case.events("card_decision") == []
    assert await case.store.get_attempt(proposal.id) is None
    assert case.restart.deleted == []
    request = await case.store.get_tool_request(investigation_id, "call-action")
    assert request.state is ToolRequestState.REJECTED
    assert request.error_code == "budget_reserve"
    assert request.proposal_id == proposal.id
    assert json.loads(request.result["tool_message"])["write_sent"] is False
    (event,) = await case.events("action_result")
    assert event.turn_id == done.result.turn_id
    assert event.payload["card_id"] == proposal.id
    assert event.payload["proposal_status"] == "voided"
    assert event.payload["attempt_id"] is None
    assert event.payload["written"] is False
    return proposal


def test_write_reserve_blocks_action_without_write(tmp_path, action_session_ids):
    async def run():
        async with action_case(
            tmp_path,
            action_session_ids,
            responses=[*_preamble(), report_call(confirmed=False, conclusion="动作预算不足，未写")],
            investigation_budget_action_seconds=10,
        ) as case:
            done = await _drive(case)
            proposal = await _assert_budget_voided(case, done)
            approval = await case.store.get_approval(proposal.id)
            assert approval.actor.startswith("policy:")
            assert approval.revoked_at is not None
            assert _action_message(case.model.seen[-1], "budget_reserve")["write_sent"] is False
            assert (await case.store.get_task(_investigation_id(done))).outcome is (
                TaskOutcome.INCONCLUSIVE
            )

    asyncio.run(run())


def test_budget_exhaustion_after_recovery_keeps_recovered_outcome(tmp_path, action_session_ids):
    async def run():
        async with action_case(
            tmp_path,
            action_session_ids,
            responses=[*_preamble(), PODS],
            investigation_budget_model_calls=6,
        ) as case:
            done = await _drive(case)
            _, attempt = await _assert_sent(case, done)
            investigation_id = _investigation_id(done)
            task = await case.store.get_task(investigation_id)
            investigation = await case.store.get_investigation(investigation_id)
            assert task.outcome is TaskOutcome.RECOVERED
            assert task.recovered_at is not None
            assert attempt.verification["outcome"] == "improved"
            assert task.used_model_calls == 6
            assert investigation.status is InvestigationStatus.INCONCLUSIVE
            assert investigation.conclusion is None
            assert task.termination_code == TerminationCode.BUDGET_EXHAUSTED
            assert done.result.payload["termination_code"] == task.termination_code
            assert done.exit_code == 4

    asyncio.run(run())


def test_approval_is_not_created_without_feedback_model_budget(tmp_path, action_session_ids):
    async def run():
        async with action_case(
            tmp_path,
            action_session_ids,
            responses=_preamble(),
            namespace="vigil-shop",
            investigation_budget_model_calls=5,
        ) as case:
            done = await _drive(case)
            proposal = await _assert_budget_voided(case, done)
            assert await case.store.get_approval(proposal.id) is None
            assert await case.safety.get_card(proposal.id, session_id=case.session_id) is None
            task = await case.store.get_task(_investigation_id(done))
            assert task.phase is TaskPhase.TERMINAL
            assert task.termination_code == TerminationCode.BUDGET_EXHAUSTED
            assert done.result.payload["termination_code"] == task.termination_code
            assert done.exit_code == 4

    asyncio.run(run())


def test_agent_turns_failed_action_into_needs_human(tmp_path, action_session_ids):
    async def run():
        async with action_case(
            tmp_path,
            action_session_ids,
            responses=[*_preamble(), _valid_report],
            namespace="vigil-shop",
            restart=FakeRestartAdapter(baseline_status=500, rebuilt_status=500),
        ) as case:
            pause, _ = await _paused(case)
            await _decide(case.safety, pause.card)
            done = await _drive(case)
            proposal, attempt = await _assert_sent(case, done, attempt_status=AttemptStatus.FAILED)
            assert proposal.status is ProposalStatus.FAILED
            assert attempt.verification["outcome"] == "no_improvement"
            task = await case.store.get_task(_investigation_id(done))
            assert task.outcome is TaskOutcome.NEEDS_HUMAN
            assert task.termination_code == TerminationCode.ACTION_NO_IMPROVEMENT
            assert done.result.payload["termination_code"] == task.termination_code
            assert done.exit_code == 0
            (followup,) = await case.store.list_followups(proposal_id=proposal.id)
            assert followup.attempt_id == attempt.id

    asyncio.run(run())


def test_two_concurrent_resumes_write_once(tmp_path, action_session_ids):
    async def run():
        async with action_case(
            tmp_path,
            action_session_ids,
            responses=[*_preamble(), _valid_report],
            namespace="vigil-shop",
        ) as case:
            pause, proposal = await _paused(case)
            approved = await _decide(case.safety, pause.card)
            async with case.driver.claim(case.session_id) as winner:
                assert winner is not None
                drive = asyncio.create_task(winner.drive())
                try:
                    async with case.driver.claim(case.session_id) as contender:
                        assert contender is None
                finally:
                    done = await drive
            assert done.exit_code == 0
            assert await _drive(case) is None
            _, attempt = await _assert_sent(case, done)
            assert attempt.proposal_id == proposal.id
            assert attempt.snapshot["approval_hash"] == approved.proposal_hash
            assert len(await case.events("card_decision")) == 1

    asyncio.run(run())
