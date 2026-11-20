"""在模型边界提交队列消息及已完成的旁路线索。"""

from collections.abc import AsyncIterator

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import HumanMessage

from aiops.agent.session_runtime import RunScope
from aiops.db import Store
from aiops.sessions.models import SessionEvent
from aiops.sessions.store import SessionStore


class DialogueState(AgentState):
    # 普通 channel，和消息一起由根 checkpoint 提交。
    vigil_input_ids: list[str]
    vigil_side_seq: int


async def events(
    sessions: SessionStore, session_id: str, *, after_seq: int = 0
) -> AsyncIterator[SessionEvent]:
    while page := await sessions.list_events(session_id, after_seq=after_seq):
        for event in page:
            yield event
        after_seq = page[-1].seq


def input_message(input_id: str, text: str) -> HumanMessage:
    return HumanMessage(id=f"vigil-input-{input_id}", content=text)


class DialogueMiddleware(AgentMiddleware):
    state_schema = DialogueState

    async def abefore_model(self, state, runtime):
        scope: RunScope = runtime.context.scope
        await runtime.context.ensure_active()
        incoming = await scope.sessions.pending_inputs(scope.lease, scope.turn.id)
        messages = [input_message(item.id, item.text) for item in incoming]
        state_update = {"messages": messages, "vigil_input_ids": [item.id for item in incoming]}
        if scope.turn.lane == "side":
            return state_update

        # 旁路答复与对应问题作为线索送入；水位和消息同时进入 checkpoint。
        seq = state.get("vigil_side_seq", 0)
        side_message = await _side_feedback(scope, seq)
        for event in side_message:
            messages.append(
                HumanMessage(
                    id=f"vigil-side-{event.turn_id}",
                    content=(
                        "已完成的只读旁路问答，仅作线索，请按本轮上下文重新核实。\n"
                        f"问题：{event.payload['side_question']}\n"
                        f"回答：{event.payload['summary']}"
                    ),
                )
            )
        state_update["vigil_side_seq"] = side_message[-1].seq if side_message else seq
        return state_update


async def _side_feedback(scope: RunScope, after_seq: int) -> list[SessionEvent]:
    session = await scope.sessions.get_session(scope.turn.session_id)
    found = []
    async for event in events(scope.sessions, scope.turn.session_id, after_seq=after_seq):
        if event.seq > session.last_seq:
            break
        if event.type == "result" and event.payload.get("side_question"):
            found.append(event)
    return found


async def paused_history(scope: RunScope, store: Store) -> list[str]:
    """读取旁路创建时最近一次卡片发布之前的公开记录，按固定边界恢复。"""
    visible = []
    stop = None
    async for event in events(scope.sessions, scope.turn.session_id):
        if event.ts >= scope.turn.created_at:
            break
        visible.append(event)
        if event.type == "approval_required":
            stop = event
    if stop is None:
        return []
    delivered = {
        event.payload["input_id"]
        for event in visible
        if event.seq <= stop.seq
        and event.type == "input_delivered"
        and event.payload["lane"] == "main"
    }
    investigation_id = (
        await store.get_investigation_id_for_turn(stop.turn_id) if stop.turn_id else None
    )
    summary = []
    for event in visible:
        if event.seq > stop.seq:
            break
        data = event.payload
        if event.type == "message" and (
            data.get("lane") == "main" or data.get("input_id") in delivered
        ):
            summary.append(f"{data['role']}：{data['text']}")
        elif event.turn_id == stop.turn_id and event.type in {
            "tool_call",
            "hypothesis",
            "evidence",
        }:
            if data.get("status") == "running":
                continue
            observation_id = data.get("observation_id")
            observation = await store.get_observation(observation_id) if observation_id else None
            if observation is not None and observation.investigation_id == investigation_id:
                text = observation.summary.split("\n原始引用：", 1)[0][:600]
                summary.append(
                    f"观测 {observation.id}（{data.get('collected_at') or event.ts.isoformat()}）："
                    f"{scope.project_text(text)}"
                )
            else:
                summary.append(f"{event.type} {event.ts.isoformat()}：{data}")
    return summary[-40:]
