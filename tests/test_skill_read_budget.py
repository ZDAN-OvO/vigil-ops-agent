"""Skill 文件读取同样受共享预算、取消与持久化重放边界约束。"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from investigation_support import call, report_call
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from test_runtime_boundaries import runtime_case

from aiops.contracts import TerminationCode, ToolRequest, ToolRequestState


def responses_for(reads, delegated):
    if delegated:
        return [
            call("task", {"description": "调查", "subagent_type": "general-purpose"}, "delegate"),
            *reads,
            AIMessage(content="子调查结束"),
            report_call(confirmed=False, conclusion="证据不足"),
        ]
    return [*reads, report_call(confirmed=False, conclusion="证据不足")]


def read_call(call_id):
    return call("read_file", {"file_path": "/description-only/SKILL.md"}, call_id)


def read_results(model):
    return {
        m.tool_call_id: m
        for batch in model.seen
        for m in batch
        if isinstance(m, ToolMessage) and m.name == "read_file"
    }


@pytest.mark.parametrize("delegated", [False, True])
@pytest.mark.parametrize("batched", [False, True])
def test_skill_reads_obey_shared_tool_budget(tmp_path, delegated, batched):
    async def run():
        reads = [read_call("first"), read_call("second")]
        if batched:
            reads = [AIMessage(content="", tool_calls=[r.tool_calls[0] for r in reads])]
        runtime, store, _, catalog = runtime_case(tmp_path, responses_for(reads, delegated))
        runtime._investigation.budget.max_tool_calls = 1
        backend = runtime._skill_backend()
        backend.aread = AsyncMock(wraps=backend.aread)
        runtime._skill_backend = lambda: backend
        _, ctx = await runtime._open_context()
        await runtime._invoke(
            runtime._build_graph(InMemorySaver(), catalog),
            {"messages": [HumanMessage(content="调查")]},
            ctx,
        )
        results = read_results(runtime._model)
        assert results["first"].status == "success"
        assert "正文标记" in results["first"].content
        assert results["second"].status == "error"
        assert "budget_exhausted" in results["second"].content
        assert ctx.budget.used_tool_calls == 1
        assert backend.aread.await_count == 1
        records = await store.list_tool_requests(ctx.investigation_id)
        assert {r.tool_call_id: r.state for r in records} == {
            "first": ToolRequestState.OBSERVED,
            "second": ToolRequestState.REJECTED,
        }

    asyncio.run(run())


@pytest.mark.parametrize("delegated", [False, True])
@pytest.mark.parametrize("pending", [False, True])
def test_skill_read_does_not_repeat_failed_or_inflight_request(tmp_path, delegated, pending):
    async def run():
        missing = call("read_file", {"file_path": "/description-only/missing.md"}, "same")
        runtime, store, _, catalog = runtime_case(tmp_path, responses_for([missing], delegated))
        backend = runtime._skill_backend()
        backend.aread = AsyncMock(wraps=backend.aread)
        runtime._skill_backend = lambda: backend
        if pending:
            await store.start_tool_request(
                ToolRequest(
                    investigation_id="runtime-boundaries",
                    tool_call_id="same",
                    tool="read_file",
                    arguments=missing.tool_calls[0]["args"],
                    state=ToolRequestState.RUNNING,
                    started_at=datetime.now(UTC),
                )
            )
        _, ctx = await runtime._open_context()
        await runtime._invoke(
            runtime._build_graph(InMemorySaver(), catalog),
            {"messages": [HumanMessage(content="首次读取")]},
            ctx,
        )
        first = read_results(runtime._model)["same"]
        assert first.status == "error"
        if pending:
            assert "request_in_flight" in first.content
        (tmp_path / "skills" / "description-only" / "missing.md").write_text("不应被重读")
        runtime._model.responses = responses_for([missing], delegated)
        runtime._model.seen = []
        _, restored = await runtime._open_context()
        await runtime._invoke(
            runtime._build_graph(InMemorySaver(), catalog),
            {"messages": [HumanMessage(content="重放读取")]},
            restored,
        )
        assert read_results(runtime._model)["same"].content == first.content
        assert backend.aread.await_count == (0 if pending else 1)
        assert restored.budget.used_tool_calls == (0 if pending else 1)

    asyncio.run(run())


def test_child_skill_read_spends_parent_budget(tmp_path):
    async def run():
        responses = responses_for([read_call("child")], True)
        responses[-1:-1] = [read_call("parent")]
        runtime, _, _, catalog = runtime_case(tmp_path, responses)
        runtime._investigation.budget.max_tool_calls = 1
        _, ctx = await runtime._open_context()
        await runtime._invoke(
            runtime._build_graph(InMemorySaver(), catalog),
            {"messages": [HumanMessage(content="调查")]},
            ctx,
        )
        results = read_results(runtime._model)
        assert results["child"].status == "success"
        assert results["parent"].status == "error"
        assert "budget_exhausted" in results["parent"].content
        assert ctx.budget.used_tool_calls == 1

    asyncio.run(run())


@pytest.mark.parametrize("delegated", [False, True])
def test_skill_read_replays_saved_result_after_context_restore(tmp_path, delegated):
    async def run():
        runtime, store, _, catalog = runtime_case(
            tmp_path, responses_for([read_call("same")], delegated)
        )
        runtime._investigation.budget.max_tool_calls = 1
        backend = runtime._skill_backend()
        backend.aread = AsyncMock(wraps=backend.aread)
        runtime._skill_backend = lambda: backend
        _, initial = await runtime._open_context()
        await runtime._invoke(
            runtime._build_graph(InMemorySaver(), catalog),
            {"messages": [HumanMessage(content="首次读取")]},
            initial,
        )
        first = read_results(runtime._model)["same"]
        assert "正文标记" in first.content
        (tmp_path / "skills" / "description-only" / "SKILL.md").write_text("磁盘内容已变化")
        runtime._model.responses = responses_for([read_call("same")], delegated)
        runtime._model.seen = []
        _, restored = await runtime._open_context()
        await runtime._invoke(
            runtime._build_graph(InMemorySaver(), catalog),
            {"messages": [HumanMessage(content="重放调用")]},
            restored,
        )
        replayed = read_results(runtime._model)["same"]
        assert replayed.content == first.content
        assert replayed.status == first.status
        assert restored.budget.used_tool_calls == 1
        assert backend.aread.await_count == 1
        assert len(await store.list_tool_requests(restored.investigation_id)) == 1

    asyncio.run(run())


@pytest.mark.parametrize("delegated", [False, True])
def test_cancel_before_skill_read_never_opens_file(tmp_path, delegated):
    async def run():
        runtime, store, rows, catalog = runtime_case(tmp_path, [])

        def cancel_then_read(_messages):
            store.get_task.return_value.cancel_requested_at = datetime.now(UTC)
            return read_call("cancelled")

        runtime._model.responses = responses_for([cancel_then_read], delegated)
        runtime._abort = AsyncMock(return_value="cancelled")
        backend = runtime._skill_backend()
        backend.aread = AsyncMock(wraps=backend.aread)
        runtime._skill_backend = lambda: backend
        _, ctx = await runtime._open_context()
        result = await runtime._invoke(
            runtime._build_graph(InMemorySaver(), catalog),
            {"messages": [HumanMessage(content="调查")]},
            ctx,
        )
        assert result == "cancelled"
        assert runtime._abort.await_args.args[1] is TerminationCode.CANCELLED
        backend.aread.assert_not_awaited()
        assert ctx.budget.used_tool_calls == 0
        if delegated:
            assert rows[0].status.value == "failed"

    asyncio.run(run())


def test_partial_snapshot_uses_actual_read_and_full_redaction_context(tmp_path):
    from aiops.agent.skill_reader import (
        SkillFilesystemBackend,
        _skill_event,
        _skill_read,
        _SkillRead,
    )
    from aiops.skills import parse_skill

    path = tmp_path / "snapshot" / "SKILL.md"
    path.parent.mkdir()
    header = "---\nname: snapshot\ndescription: 核对上下文\n---\n"
    path.write_text(header + "复核旧版\n", encoding="utf-8")
    spec = parse_skill(path)
    backend = SkillFilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)
    virtual = "/snapshot/SKILL.md"

    async def capture(offset, limit):
        read = _SkillRead(virtual)
        token = _skill_read.set(read)
        try:
            result = await backend.aread(virtual, offset, limit)
        finally:
            _skill_read.reset(token)
        assert not result.error
        text = result.file_data["content"].removesuffix("\n")
        window = f"{result.start_line}-{result.end_line} of {result.total_lines}"
        return _skill_event(
            spec,
            ToolMessage(
                tool_call_id="read",
                content=f"@@ lines {window} @@\n{text}",
            ),
            read.content,
        )

    path.write_text(header + "复核新版 /home/fixture/private.md\n", encoding="utf-8")
    changed = asyncio.run(capture(4, 1))
    assert "content_hash" not in changed
    assert changed["snapshot"] == {
        "text": "复核新版 [已遮盖]",
        "start_line": 5,
        "end_line": 5,
        "truncated": False,
    }

    marker = "multi-line-fixture-only"
    path.write_text(
        header
        + "-----BEGIN "
        + "PRIVATE KEY-----\n"
        + marker
        + "\n-----END "
        + "PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    within_key = asyncio.run(capture(5, 1))
    assert "snapshot" not in within_key and marker not in repr(within_key)

    path.write_text(header + f"access_token: |\n  {marker}\n", encoding="utf-8")
    within_yaml = asyncio.run(capture(5, 1))
    assert "snapshot" not in within_yaml and marker not in repr(within_yaml)
