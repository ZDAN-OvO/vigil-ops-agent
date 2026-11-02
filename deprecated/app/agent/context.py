"""Agent 运行时上下文

定义 Agent 执行期间的运行时上下文数据，
供 LangGraph Middleware 通过 runtime.context 访问。

使用方式：
    config = {"configurable": {"thread_id": session_id}}
    context = AgentContext(user_id="123")
    await agent.ainvoke(input, config=config, context=context)

在 Middleware 中：
    user_id = runtime.context.user_id
"""

from dataclasses import dataclass


@dataclass
class AgentContext:
    """Agent 运行时上下文

    Attributes:
        user_id: 当前用户 ID（字符串形式），用于长期记忆的 Store 读写。
                 为 None 时跳过用户画像加载/保存。
    """

    user_id: str | None = None
