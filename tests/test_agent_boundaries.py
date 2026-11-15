"""离线动作边界回归：真实资源关联和 checkpoint 恢复后的停止状态。"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from investigation_support import ScriptedChatModel, settings
from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from aiops.actions.rollout_undo import ActionError
from aiops.agent.context import AgentContext
from aiops.agent.engine import LangChainInvestigation
from aiops.agent.middleware import ActionBatchMiddleware, BudgetMiddleware, ProgressMiddleware
from aiops.agent.schemas import RequestActionArgs
from aiops.agent.tools import _resume_pending_action, _validate_action_references, request_action
from aiops.contracts import (
    AttemptStatus,
    Budget,
    InvestigationTask,
    ObservationStatus,
    ProposalStatus,
    TaskPhase,
    TimeRange,
    ToolRequestState,
)
from aiops.db import Store
from aiops.tools.base import ToolContext

ARGS = {
    "action": "restart_pod",
    "target_pod": "cartservice-abc-123",
    "service": "cartservice",
    "reason": "目标 Pod 异常",
    "hypothesis_id": "h1",
    "evidence_ids": ["obs-pod", "obs-metric"],
}


def reference_store(tmp_path, *, selector="app=cartservice", name=None, namespace="shop"):
    name = name or ARGS["target_pod"]
    tool_context = ToolContext.create(
        investigation_id="inv1",
        cluster="lab",
        namespace=namespace,
        runtime_path=tmp_path,
        timeout_seconds=10,
    )
    raw = tool_context.persist_raw(
        "pods",
        {
            "query": {"kind": "pods", "namespace": namespace, "selector": selector},
            "items": [{"name": name, "uid": "pod-uid"}],
        },
    )
    store = AsyncMock(spec=Store)
    store.get_hypothesis.return_value = SimpleNamespace(
        id="h1",
        investigation_id="inv1",
        status=SimpleNamespace(value="supported"),
    )
    store.list_observations.return_value = [
        SimpleNamespace(
            id="obs-pod",
            tool_call_id="inv1:pods",
            source="kubernetes",
            status=ObservationStatus.OK,
            raw_ref=raw.ref,
            summary='{"items": [{"name": "fake-summary-target"}]}',
        ),
        SimpleNamespace(
            id="obs-metric",
            tool_call_id="inv1:metric",
            source="prometheus",
            status=ObservationStatus.OK,
        ),
    ]
    store.list_hypothesis_evidence.return_value = [
        SimpleNamespace(observation_id=oid, hypothesis_id="h1", relation="supporting")
        for oid in ARGS["evidence_ids"]
    ]
    store.list_tool_calls.return_value = [
        SimpleNamespace(
            id="inv1:pods", tool="kubernetes.list_pods", parameters={"selector": selector}
        ),
        SimpleNamespace(id="inv1:metric", tool="prometheus.query_range", parameters={}),
    ]
    return store


def test_action_accepts_actual_target_from_label_query(tmp_path):
    store = reference_store(tmp_path)
    ctx = SimpleNamespace(
        store=store, investigation_id="inv1", namespace="shop", runtime_path=tmp_path
    )
    assert asyncio.run(_validate_action_references(ctx, RequestActionArgs(**ARGS))) is None


@pytest.mark.parametrize(
    "name,namespace,ref",
    [
        ("unrelated-pod", "shop", "valid"),
        (ARGS["target_pod"], "other-namespace", "valid"),
        (ARGS["target_pod"], "shop", "missing"),
        (ARGS["target_pod"], "shop", "outside"),
        (ARGS["target_pod"], "shop", "malformed"),
    ],
)
def test_action_rejects_selector_substring_without_target_identity(
    tmp_path,
    name,
    namespace,
    ref,
):
    store = reference_store(
        tmp_path, selector=f"app={ARGS['target_pod']}-debug", name=name, namespace=namespace
    )
    observed = store.list_observations.return_value[0]
    if ref == "missing":
        observed.raw_ref = "evidence/inv1/missing.json"
    elif ref == "outside":
        outside = tmp_path / "outside.json"
        outside.write_text((tmp_path / observed.raw_ref).read_text())
        observed.raw_ref = "outside.json"
    elif ref == "malformed":
        (tmp_path / observed.raw_ref).write_text("{broken")
    ctx = SimpleNamespace(
        store=store, investigation_id="inv1", namespace="shop", runtime_path=tmp_path
    )
    assert asyncio.run(_validate_action_references(ctx, RequestActionArgs(**ARGS))) is not None


def runtime_for(store, tmp_path):
    now = datetime.now(UTC)
    store.get_task.return_value = InvestigationTask(
        investigation_id="inv1",
        phase=TaskPhase.RUNNING,
        runtime_version="test",
        calendar_started_at=now,
    )
    return LangChainInvestigation(
        settings=settings(),
        store=store,
        dsn="unused",
        incident=SimpleNamespace(
            namespace="shop",
            cluster="lab",
            time_range=TimeRange(start=now - timedelta(minutes=1), end=now),
        ),
        investigation=SimpleNamespace(
            id="inv1",
            budget=Budget(max_tool_calls=20, max_model_calls=20, max_wall_clock_seconds=600),
        ),
        toolbox=None,
        executor=None,
        runtime_path=tmp_path,
        model=ScriptedChatModel(responses=[]),
    )


def test_open_context_restores_persisted_recovery(tmp_path):
    store = AsyncMock(spec=Store)
    runtime = runtime_for(store, tmp_path)
    store.get_task.return_value.recovered_at = datetime.now(UTC)
    _, ctx = asyncio.run(runtime._open_context())
    assert ctx.progress.recovered is True


@pytest.mark.parametrize(
    "status,outcome,expected",
    [
        (AttemptStatus.VERIFIED, "improved", True),
        (AttemptStatus.UNKNOWN, "improved", False),
        (AttemptStatus.VERIFIED, "unknown", False),
    ],
)
def test_replayed_attempt_restores_only_verified_recovery(tmp_path, status, outcome, expected):
    async def run():
        store = AsyncMock(spec=Store)
        runtime = runtime_for(store, tmp_path)
        _, ctx = await runtime._open_context()
        store.get_proposal.return_value = proposal()
        store.get_attempt.return_value = SimpleNamespace(
            id="attempt1",
            status=status,
            verification={"outcome": outcome},
            result={},
        )
        store.list_followups.return_value = []
        await _resume_pending_action(ctx, "first", SimpleNamespace(proposal_id="proposal1"))
        return ctx, store

    ctx, store = asyncio.run(run())
    assert ctx.progress.recovered is expected
    assert store.record_recovery.await_count == int(expected)


def proposal():
    return SimpleNamespace(
        id="proposal1",
        kind=SimpleNamespace(value="restart_pod"),
        purpose=SimpleNamespace(value="remediation"),
        target=SimpleNamespace(namespace="shop", kind="Pod", name=ARGS["target_pod"]),
        prediction="恢复",
        observation_window_seconds=90,
        status=ProposalStatus.PENDING_APPROVAL,
    )


@pytest.mark.parametrize("persisted_recovery", [True, False])
def test_checkpoint_replay_after_verified_action_blocks_second_write(tmp_path, persisted_recovery):
    async def run():
        store = reference_store(tmp_path, selector=f"app={ARGS['target_pod']}")
        runtime = runtime_for(store, tmp_path)
        saved_proposal = proposal()
        store.get_proposal.return_value = saved_proposal
        store.get_attempt.return_value = None
        store.get_approval.return_value = None
        store.list_followups.return_value = []
        requests = {
            "first": SimpleNamespace(
                state=ToolRequestState.ACTION_PENDING,
                result={},
                proposal_id=saved_proposal.id,
            )
        }
        store.get_tool_request.side_effect = lambda inv, call: requests.get(call)

        async def finish(inv, call, **values):
            requests[call] = SimpleNamespace(**values)

        async def start(request):
            requests[request.tool_call_id] = request

        store.finish_tool_request.side_effect = finish
        store.start_tool_request.side_effect = start
        executor = SimpleNamespace(
            prepare_restart_pod=AsyncMock(
                side_effect=ActionError("unexpected_write", "恢复后不应进入新动作准备"),
            )
        )
        runtime._executor = executor
        messages = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "request_action",
                        "id": call,
                        "args": ARGS,
                    }
                ],
            )
            for call in ("first", "second")
        ]
        model = ScriptedChatModel(responses=[*messages, AIMessage(content="done")])
        graph = create_agent(
            model,
            tools=[request_action],
            context_schema=AgentContext,
            checkpointer=InMemorySaver(),
            middleware=[
                ActionBatchMiddleware(),
                BudgetMiddleware(configured_timeout=10, max_retries=0),
                ProgressMiddleware(),
            ],
        )
        cfg = {"configurable": {"thread_id": "recovery-test"}}
        _, initial = await runtime._open_context()
        first = await graph.ainvoke(
            {"messages": [{"role": "user", "content": "调查"}]}, cfg, context=initial
        )
        assert first.get("__interrupt__")
        # 写操作与 recovered_at 已提交，图仍停留在动作工具之前的审批 checkpoint。
        store.get_attempt.return_value = SimpleNamespace(
            id="attempt1",
            status=AttemptStatus.VERIFIED,
            verification={"outcome": "improved"},
            result={},
        )
        saved_proposal.status = ProposalStatus.SUCCEEDED
        store.get_task.return_value.recovered_at = datetime.now(UTC) if persisted_recovery else None
        _, resumed = await runtime._open_context()
        await graph.ainvoke(Command(resume={"decision": "approved"}), cfg, context=resumed)
        return resumed, executor

    ctx, executor = asyncio.run(run())
    assert ctx.progress.recovered is True
    executor.prepare_restart_pod.assert_not_awaited()
