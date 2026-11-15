"""离线回归：Skill 读取权限、委派终止、字典行计数与瞬态读取重试。"""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from investigation_support import ScriptedChatModel, call, report_call, settings
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from psycopg.pq import ExecStatus
from psycopg.rows import dict_row

from aiops.agent.delegation import CURRENT_CONTEXT, DelegationFailure, build_task_runnable
from aiops.agent.engine import LangChainInvestigation
from aiops.agent.tools import _query, _run_read
from aiops.contracts import (
    Budget,
    DelegationStatus,
    InvestigationTask,
    SkillTrigger,
    TaskPhase,
    TerminationCode,
    TimeRange,
    ToolError,
    ToolRequestState,
    ToolResult,
)
from aiops.db import Store
from aiops.skills import detect_loaded_skills, load_skill_catalog


class CountPool:
    """用真实 dict_row 工厂生成 PostgreSQL count 结果，不连接数据库。"""

    def __init__(self, count):
        self.count = count
        self.queries = []

    @asynccontextmanager
    async def connection(self):
        async def execute(sql, params):
            self.queries.append((sql, params))
            name = b"failed_count" if "AS failed_count" in sql else b"count"
            cursor = AsyncMock()
            cursor.pgresult = SimpleNamespace(
                nfields=1, status=ExecStatus.TUPLES_OK, fname=lambda _: name
            )
            cursor._encoding = "utf-8"
            cursor.fetchone.return_value = dict_row(cursor)([self.count()])
            return cursor

        yield SimpleNamespace(execute=execute)


def runtime_case(tmp_path, responses):
    now = datetime.now(UTC)
    store = AsyncMock(spec=Store)
    store.get_task.return_value = InvestigationTask(
        investigation_id="runtime-boundaries",
        phase=TaskPhase.RUNNING,
        runtime_version="test",
        calendar_started_at=now,
    )
    rows = []
    store.create_delegation.side_effect = rows.append

    async def finish(delegation_id, **values):
        row = next(row for row in rows if row.id == delegation_id)
        for key, value in values.items():
            setattr(row, key, value)

    store.finish_delegation.side_effect = finish
    store.count_failed_delegations.side_effect = lambda _: sum(
        row.status is DelegationStatus.FAILED for row in rows
    )
    runtime = LangChainInvestigation(
        settings=settings(skills_dir=tmp_path / "skills"),
        store=store,
        dsn="unused",
        incident=SimpleNamespace(
            namespace="shop",
            cluster="lab",
            time_range=TimeRange(start=now - timedelta(minutes=1), end=now),
        ),
        investigation=SimpleNamespace(
            id="runtime-boundaries",
            budget=Budget(max_tool_calls=20, max_model_calls=20, max_wall_clock_seconds=600),
        ),
        toolbox=None,
        executor=None,
        runtime_path=tmp_path,
        model=ScriptedChatModel(responses=responses),
    )
    skill = tmp_path / "skills" / "description-only" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: description-only\ndescription: 调查特定症状时读取\n---\n"
        "正文标记：先核实实际资源身份。\n"
    )
    return runtime, store, rows, load_skill_catalog(tmp_path / "skills")


@pytest.mark.parametrize("delegated", [False, True])
def test_description_skill_body_is_readable_and_recorded(tmp_path, delegated):
    async def run():
        responses = [call("read_file", {"file_path": "/description-only/SKILL.md"}, "read")]
        if delegated:
            responses = [
                call(
                    "task",
                    {"description": "按描述读取相关 Skill", "subagent_type": "general-purpose"},
                    "delegate",
                ),
                *responses,
                # 子图只返回文本，主图仍使用结构化报告。
                AIMessage(content="已按描述读取正文"),
            ]
        responses.append(report_call(confirmed=False, conclusion="证据不足"))
        runtime, store, rows, catalog = runtime_case(tmp_path, responses)
        _, ctx = await runtime._open_context()
        graph = runtime._build_graph(InMemorySaver(), catalog)
        result = await runtime._invoke(
            graph, {"messages": [HumanMessage(content="调查特定症状")]}, ctx
        )
        assert not catalog[0].sources
        if delegated:
            assert rows[0].status is DelegationStatus.SUCCEEDED
            assert rows[0].skills == ["description-only"]
        else:
            reads = [
                m
                for m in result["messages"]
                if isinstance(m, ToolMessage) and m.name == "read_file"
            ]
            assert len(reads) == 1
            assert "正文标记：先核实实际资源身份。" in reads[0].content
            assert detect_loaded_skills(reads, catalog) == catalog
            store.get_investigation.return_value = SimpleNamespace(skills=[])
            await runtime._record_loaded_skills(catalog, result)
            usage = store.set_investigation_skills.await_args.args[1][0]
            assert usage.name == "description-only"
            assert usage.trigger is SkillTrigger.DESCRIPTION

    asyncio.run(run())


@pytest.mark.parametrize("delegated", [False, True])
@pytest.mark.parametrize("path_kind", ["traversal", "absolute", "symlink"])
def test_skill_reader_rejects_paths_outside_root(tmp_path, delegated, path_kind):
    async def run():
        secret = tmp_path / "secret.txt"
        secret.write_text("private-boundary-marker")
        path = {"traversal": "/../secret.txt", "absolute": str(secret), "symlink": "/leak.txt"}[
            path_kind
        ]
        runtime, _, _, catalog = runtime_case(
            tmp_path,
            [
                call("read_file", {"file_path": path}, "denied"),
                report_call(confirmed=False, conclusion="证据不足"),
            ],
        )
        (tmp_path / "skills" / "leak.txt").symlink_to(secret)
        _, ctx = await runtime._open_context()
        if delegated:
            graph = runtime._build_subagent_app(catalog)
            # 子图无结构化报告约束。
            runtime._model.responses[-1] = AIMessage(content="结束")
        else:
            graph = runtime._build_graph(InMemorySaver(), catalog)
        try:
            result = await graph.ainvoke(
                {"messages": [HumanMessage(content="读取文件")]}, runtime._config(), context=ctx
            )
        except ValueError as exc:
            assert path_kind == "symlink"
            assert "outside root directory" in str(exc)
        else:
            reads = [
                m
                for m in result["messages"]
                if isinstance(m, ToolMessage) and m.name == "read_file"
            ]
            assert len(reads) == 1
            assert reads[0].status == "error"
            assert "private-boundary-marker" not in reads[0].content
        names = set(graph.nodes["tools"].bound.tools_by_name)
        assert "read_file" in names
        assert not names.intersection({"write_file", "edit_file", "execute", "delete"})
        assert secret.read_text() == "private-boundary-marker"

    asyncio.run(run())


def test_first_delegated_write_violation_aborts_parent_and_audits(tmp_path):
    async def run():
        responses = [
            call(
                "task", {"description": "只读调查", "subagent_type": "general-purpose"}, "delegate"
            ),
            call("request_action", {}, "forbidden"),
            report_call(confirmed=False, conclusion="不应继续"),
        ]
        runtime, store, rows, catalog = runtime_case(tmp_path, responses)
        runtime._abort = AsyncMock(return_value="aborted")
        _, ctx = await runtime._open_context()
        result = await runtime._invoke(
            runtime._build_graph(InMemorySaver(), catalog),
            {"messages": [HumanMessage(content="调查")]},
            ctx,
        )
        assert result == "aborted"
        assert len(runtime._model.seen) == 2
        assert len(rows) == 1 and rows[0].status is DelegationStatus.FAILED
        runtime._abort.assert_awaited_once()
        assert runtime._abort.await_args.args[1] is TerminationCode.READ_ONLY_VIOLATION
        store.append_audit.assert_awaited_once()
        assert store.append_audit.await_args.args[0][0].event == "delegation.read_only_violation"

    asyncio.run(run())


@pytest.mark.parametrize("count", [0, 1, 2])
def test_failed_delegation_count_uses_dict_row(count):
    pool = CountPool(lambda: count)
    assert asyncio.run(Store(pool).count_failed_delegations("inv1")) == count
    assert pool.queries[0][1] == ("inv1", "failed")


def test_ordinary_delegation_failure_counts_and_stops_on_second(tmp_path):
    async def run():
        runtime, store, rows, catalog = runtime_case(tmp_path, [])
        pool = CountPool(lambda: sum(row.status is DelegationStatus.FAILED for row in rows))
        store.count_failed_delegations.side_effect = Store(pool).count_failed_delegations
        _, ctx = await runtime._open_context()
        app = SimpleNamespace(ainvoke=AsyncMock(side_effect=RuntimeError("ordinary failure")))
        runnable = build_task_runnable(app, catalog=catalog)
        token = CURRENT_CONTEXT.set(ctx)
        try:
            first = await runnable.ainvoke({"messages": [HumanMessage(content="调查")]})
            assert "已记录失败" in first["messages"][-1].content
            with pytest.raises(DelegationFailure, match="2 次"):
                await runnable.ainvoke({"messages": [HumanMessage(content="再调查")]})
        finally:
            CURRENT_CONTEXT.reset(token)
        assert len(rows) == 2
        assert all(row.status is DelegationStatus.FAILED for row in rows)

    asyncio.run(run())


@pytest.mark.parametrize("retryable", [True, False, None])
def test_read_retry_preserves_call_id_and_nontransient_cache(tmp_path, retryable):
    async def run():
        runtime, store, _, _ = runtime_case(tmp_path, [])
        _, ctx = await runtime._open_context()
        requests = {}
        store.list_tool_requests.side_effect = lambda _: list(requests.values())
        store.get_tool_request.side_effect = lambda _, call_id: requests.get(call_id)

        async def start(request):
            requests[request.tool_call_id] = request

        async def finish(_, call_id, **values):
            for key, value in values.items():
                if hasattr(requests[call_id], key):
                    setattr(requests[call_id], key, value)

        store.start_tool_request.side_effect = start
        store.finish_tool_request.side_effect = finish

        def result(error=None):
            return ToolResult(
                tool_call_id="adapter",
                source="kubernetes",
                query={},
                time_range=ctx.window,
                collected_at=datetime.now(UTC),
                data=None if error else {"items": [{"name": "target-pod"}]},
                error=error,
            )

        error = (
            None
            if retryable is None
            else ToolError(
                code="timeout" if retryable else "forbidden", message="failed", retryable=retryable
            )
        )
        ctx = replace(
            ctx, toolbox=SimpleNamespace(run=AsyncMock(side_effect=[result(error), result()]))
        )

        async def read(call_id):
            tool_runtime = ToolRuntime(
                state={},
                context=ctx,
                config={},
                stream_writer=None,
                tool_call_id=call_id,
                store=None,
            )
            return json.loads(
                await _run_read(
                    tool_runtime,
                    tool_name="kubernetes.list_pods",
                    request=_query("kubernetes.list_pods", selector="app=target"),
                )
            )

        first = await read("first")
        assert await read("first") == first
        assert ctx.toolbox.run.await_count == 1
        second = await read("retry")
        if retryable:
            assert second["error"] is None
            assert ctx.toolbox.run.await_count == 2
            assert ctx.budget.used_tool_calls == 2
            assert len(requests) == 2
            assert requests["first"].state is ToolRequestState.FAILED
            assert requests["retry"].state is ToolRequestState.OBSERVED
            assert store.save_observation.await_count == 1
            assert await read("third") == second
            assert ctx.toolbox.run.await_count == 2
            assert await read("first") == first
            assert ctx.toolbox.run.await_count == 2
        else:
            assert second == first
            assert ctx.toolbox.run.await_count == 1
            assert ctx.budget.used_tool_calls == 1
            assert len(requests) == 1

    asyncio.run(run())
