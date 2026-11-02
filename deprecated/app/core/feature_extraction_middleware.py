"""用户画像特征提取中间件

LangGraph AgentMiddleware 实现，在 Agent 执行后自动提取用户画像：
- aafter_agent: 从对话中提取关键信息，异步保存到 Store

画像注入逻辑已迁移到 rag_agent_service.py（注入到 system prompt 开头），
确保模型优先看到用户画像信息。

依赖：
- LongTermMemoryService: Store CRUD
- FeatureExtractor: 通用特征提取
- AgentContext: 通过 runtime.context 获取 user_id

使用方式：
    middleware = FeatureExtractionMiddleware(store=pg_store, llm=model)
    agent = create_agent(model, middleware=[middleware], context_schema=AgentContext)
"""

import asyncio
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.runtime import Runtime
from langgraph.store.base import BaseStore
from loguru import logger

from app.agent.context import AgentContext
from app.core.feature_extractor import (
    FeatureExtractor,
    default_user_profile_extractor,
)
from app.services.long_term_memory_service import long_term_memory_service


class FeatureExtractionMiddleware(AgentMiddleware[Any, AgentContext, Any]):
    """用户画像特征提取中间件

    在 Agent 执行后提取对话特征并保存到 Store。
    画像加载和注入由 rag_agent_service 负责（注入到 system prompt 开头）。
    """

    def __init__(
        self,
        store: BaseStore,
        llm: BaseChatModel,
        extractor: FeatureExtractor | None = None,
    ):
        """初始化中间件

        Args:
            store: LangGraph BaseStore 实例（通常为 AsyncPostgresStore）
            llm: 用于特征提取的 LLM 实例
            extractor: 自定义特征提取器。为 None 时使用默认的用户画像提取器。
        """
        self.store = store
        self.model = llm
        self.extractor = extractor or default_user_profile_extractor(llm)

    async def aafter_agent(
        self,
        state: Any,
        runtime: Runtime[AgentContext],
    ) -> dict[str, Any] | None:
        """Agent 执行后：提取对话特征并异步保存到 Store

        从对话消息中提取用户偏好、专业水平、常问话题和关键事实，
        使用后台任务异步执行，不阻塞响应返回。

        Args:
            state: 当前 Agent 状态（包含完整消息历史）
            runtime: 运行时上下文

        Returns:
            None（不修改状态）
        """
        user_id = self._get_user_id(runtime)
        if not user_id:
            return None

        # 获取消息列表
        messages = state.get("messages", [])
        if not messages:
            return None

        # 异步执行特征提取和保存（不阻塞主流程）
        asyncio.create_task(self._extract_and_save(user_id, messages))

        return None

    async def _extract_and_save(
        self,
        user_id: str,
        messages: list,
    ) -> None:
        """后台任务：提取特征并保存到 Store

        Args:
            user_id: 用户 ID
            messages: 对话消息列表
        """
        try:
            # 加载已有画像（用于合并）
            existing_profile = await long_term_memory_service.load_user_profile(self.store, user_id)

            # 用 FeatureExtractor 提取/合并特征
            updated_profile = await self.extractor.extract(messages, existing_profile)

            # 保存到 Store
            await long_term_memory_service.save_user_profile(self.store, user_id, updated_profile)

            logger.info(f"[用户 {user_id}] 特征提取并保存完成")

        except Exception as e:
            logger.warning(f"[用户 {user_id}] 特征提取失败: {e}")

    @staticmethod
    def _get_user_id(runtime: Runtime[AgentContext]) -> str | None:
        """从 runtime.context 中安全获取 user_id

        Args:
            runtime: LangGraph 运行时上下文

        Returns:
            user_id 字符串，或 None（不可用时）
        """
        context = runtime.context
        if context is None:
            return None
        return context.user_id
