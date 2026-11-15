"""手动调查走实际 Agent 图，检查模型看到的 Skill 列表与按需正文。"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from investigation_support import call, report_call
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from test_runtime_boundaries import runtime_case

from aiops.contracts import Incident, SkillTrigger
from aiops.skills import load_skill_catalog, match_signal_skills


@pytest.mark.parametrize("delegated", [False, True])
def test_manual_model_sees_pod_rebuild_description_without_alert(tmp_path, delegated):
    async def run():
        responses = [call("read_file", {"file_path": "/pod-rebuild/SKILL.md"}, "read-skill")]
        if delegated:
            responses = [
                call(
                    "task", {"description": "核对异常", "subagent_type": "general-purpose"}, "sub"
                ),
                *responses,
                AIMessage(content="只读调查结束"),
            ]
        responses.append(report_call(confirmed=False, conclusion="证据不足"))
        runtime, store, _, _ = runtime_case(tmp_path, responses)
        runtime._settings = runtime._settings.model_copy(
            update={"skills_dir": Path(__file__).resolve().parents[1] / "skills"}
        )
        runtime._incident = Incident(
            id="manual-incident",
            environment="test",
            cluster="lab",
            namespace="shop",
            service="example-service",
            trigger="manual",
            symptoms="业务持续返回 500，Pod 仍然 Ready",
            time_range=runtime._incident.time_range,
        )

        class MemorySaver(InMemorySaver):
            async def setup(self):
                pass

        @asynccontextmanager
        async def saver():
            yield MemorySaver()

        runtime._saver = saver
        runtime._finish_graph = AsyncMock()
        store.get_investigation.return_value = None
        await runtime.run()

        catalog = load_skill_catalog(runtime._settings.skills_path)
        assert runtime._incident.alert_names == []
        assert [s.name for s in match_signal_skills(catalog, runtime._incident)] == [
            "hypothesis-driven-investigation"
        ]
        pod = next(s for s in catalog if s.name == "pod-rebuild")
        assert "Pod 仍 Ready 但业务持续报错" in pod.description
        assert "单副本" in pod.description
        assert "bad-release" in pod.description
        assert "oom" in pod.description
        assert "dependency-latency" in pod.description

        # 同时覆盖主图与委派图的首次模型输入，而非只验证 YAML 解析。
        batches = runtime._model.seen[: 2 if delegated else 1]
        for batch in batches:
            system = "\n".join(m.text for m in batch if isinstance(m, SystemMessage))
            for skill in catalog:
                assert f"- **{skill.name}**: {skill.description}" in system
                assert f"/{skill.name}/SKILL.md" in system
            human = "\n".join(m.text for m in batch if isinstance(m, HumanMessage))
            assert "# Pod 重建判断" not in human

        reads = [
            m
            for batch in runtime._model.seen
            for m in batch
            if isinstance(m, ToolMessage) and m.tool_call_id == "read-skill"
        ]
        assert reads and all(m.status == "success" for m in reads)
        assert "# Pod 重建判断" in reads[0].text
        if not delegated:
            usage = store.set_investigation_skills.call_args.args[1]
            assert any(
                s.name == "pod-rebuild" and s.trigger is SkillTrigger.DESCRIPTION for s in usage
            )

    asyncio.run(run())
