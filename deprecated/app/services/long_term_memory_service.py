"""长期记忆服务

封装 LangGraph Store 的读写操作，
供 FeatureExtractionMiddleware 和其他需要长期记忆的场景调用。

核心功能：
1. load_user_profile() - 从 Store 加载用户画像
2. save_user_profile() - 将用户画像写入 Store
3. format_profile_for_prompt() - 将画像格式化为可注入 system prompt 的文本

Store Namespace 设计：
    ("user_profiles",) + user_id → 用户画像 dict

注意：
- 特征提取逻辑已迁移到 app/core/feature_extractor.py
- 画像归约逻辑已迁移到 app/core/profile_reducer.py
"""

from datetime import UTC, datetime
from typing import Any

from langgraph.store.base import BaseStore
from loguru import logger

# 用户画像的 Store namespace
USER_PROFILES_NAMESPACE = ("user_profiles",)


class LongTermMemoryService:
    """长期记忆服务

    封装 Store CRUD 操作，提供通用的长期记忆读写接口。
    """

    async def load_user_profile(
        self,
        store: BaseStore,
        user_id: str,
    ) -> dict[str, Any] | None:
        """从 Store 加载用户画像

        Args:
            store: LangGraph BaseStore 实例
            user_id: 用户 ID

        Returns:
            用户画像 dict，不存在时返回 None
        """
        try:
            item = await store.aget(USER_PROFILES_NAMESPACE, user_id)
            if item is None:
                logger.debug(f"[用户 {user_id}] Store 中无画像")
                return None

            logger.info(f"[用户 {user_id}] 从 Store 加载画像成功")
            return item.value

        except Exception as e:
            logger.warning(f"[用户 {user_id}] 加载画像失败: {e}")
            return None

    async def save_user_profile(
        self,
        store: BaseStore,
        user_id: str,
        profile: dict[str, Any],
    ) -> bool:
        """将用户画像写入 Store

        Args:
            store: LangGraph BaseStore 实例
            user_id: 用户 ID
            profile: 用户画像 dict

        Returns:
            是否成功
        """
        try:
            # 更新时间戳
            profile["updated_at"] = datetime.now(UTC).isoformat()

            await store.aput(USER_PROFILES_NAMESPACE, user_id, profile)
            logger.info(f"[用户 {user_id}] 画像已保存到 Store")
            return True

        except Exception as e:
            logger.warning(f"[用户 {user_id}] 保存画像失败: {e}")
            return False

    @staticmethod
    def format_profile_for_prompt(profile: dict[str, Any]) -> str:
        """将用户画像格式化为可注入 system prompt 的文本

        Args:
            profile: 用户画像 dict

        Returns:
            格式化后的文本字符串
        """
        parts = []

        if profile.get("preferences"):
            parts.append(f"用户偏好: {profile['preferences']}")

        if profile.get("expertise_level"):
            parts.append(f"专业水平: {profile['expertise_level']}")

        if profile.get("common_topics"):
            topics = "、".join(profile["common_topics"][:5])
            parts.append(f"常问话题: {topics}")

        if profile.get("key_facts"):
            facts = "；".join(profile["key_facts"][:5])
            parts.append(f"关键信息: {facts}")

        if not parts:
            return ""

        return "【用户画像】\n" + "\n".join(parts)


# 全局单例
long_term_memory_service = LongTermMemoryService()
