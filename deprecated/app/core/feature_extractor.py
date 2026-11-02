"""通用特征提取服务

从对话消息中通过 LLM 提取结构化特征，支持自定义：
- extraction_prompt: 提取提示词模板
- output_schema: 输出结构定义（含字段描述和默认值）
- reducer: 归约函数（合并新旧特征的策略）

使用方式：
    from app.core.feature_extractor import FeatureExtractor, default_user_profile_extractor

    # 使用默认的用户画像提取器
    extractor = default_user_profile_extractor(llm)
    profile = await extractor.extract(messages, existing_profile)

    # 自定义提取器
    extractor = FeatureExtractor(
        llm=llm,
        extraction_prompt=CUSTOM_PROMPT,
        output_schema=CUSTOM_SCHEMA,
        reducer=custom_reducer,
    )
    result = await extractor.extract(messages, existing)
"""

import json
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from loguru import logger

from app.core.profile_reducer import ProfileReducer, merge_profile_reducer

# 用户画像默认结构
DEFAULT_PROFILE: dict[str, Any] = {
    "preferences": "",
    "expertise_level": "",
    "common_topics": [],
    "key_facts": [],
    "updated_at": "",
}

# 默认用户画像提取 prompt
DEFAULT_USER_PROFILE_PROMPT = """你是一个用户画像提取助手。请从以下对话历史中提取用户的关键特征，生成一份结构化的用户画像。

## 已有画像（用于合并，不要丢失已有信息）
{existing_profile}

## 最近对话
{recent_messages}

请输出 JSON 格式的用户画像，包含以下字段：
- preferences: 用户的偏好（语言风格、回答详细程度、关注的方面等），字符串
- expertise_level: 用户的专业水平（新手/中级/专家），字符串
- common_topics: 用户常问的话题列表，字符串数组（最多保留 10 个）
- key_facts: 对话中提到的关键事实、决策或重要信息，字符串数组（最多保留 20 个）

注意：
1. 如果已有画像中有信息，请合并新旧信息，不要丢失已有内容
2. common_topics 和 key_facts 去重后保留
3. 只输出 JSON，不要其他说明文字
"""


class FeatureExtractor:
    """通用特征提取器

    从对话消息中通过 LLM 提取结构化特征，
    支持自定义 prompt、输出 schema 和归约策略。
    """

    def __init__(
        self,
        llm: BaseChatModel,
        extraction_prompt: str,
        output_schema: dict[str, Any] | None = None,
        reducer: ProfileReducer | None = None,
        max_messages: int = 10,
    ):
        """初始化特征提取器

        Args:
            llm: 用于特征提取的 LLM 实例
            extraction_prompt: 提取提示词模板，支持占位符：
                - {existing_profile}: 已有画像的文本表示
                - {recent_messages}: 最近对话的文本表示
            output_schema: 输出结构定义，dict 格式：
                {"field_name": {"type": "str|list", "default": default_value}}
                用于确保输出包含所有必要字段。为 None 时不做 schema 校验。
            reducer: 归约函数，合并新旧特征。默认使用 merge_profile_reducer。
            max_messages: 最多分析的对话消息条数（默认 10）
        """
        self.llm = llm
        self.extraction_prompt = extraction_prompt
        self.output_schema = output_schema
        self.reducer = reducer or merge_profile_reducer
        self.max_messages = max_messages

    async def extract(
        self,
        messages: list[BaseMessage],
        existing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """从对话消息中提取特征

        Args:
            messages: 对话消息列表
            existing: 已有特征（用于合并），为 None 时使用空结构

        Returns:
            提取并归约后的特征 dict
        """
        # 过滤出 user/assistant 消息，取最近 N 条
        recent = [m for m in messages if isinstance(m, (HumanMessage, AIMessage))][
            -self.max_messages :
        ]

        if not recent:
            logger.debug("无可提取的对话消息")
            return existing or self._default_output()

        # 格式化消息文本
        messages_text = "\n".join(
            f"{'用户' if isinstance(m, HumanMessage) else '助手'}: {m.content}"
            for m in recent
            if m.content
        )

        # 格式化已有特征
        if existing:
            existing_text = "\n".join(f"- {k}: {v}" for k, v in existing.items() if v)
        else:
            existing_text = "（无已有画像）"

        prompt = self.extraction_prompt.format(
            existing_profile=existing_text,
            recent_messages=messages_text,
        )

        try:
            response = await self.llm.ainvoke([HumanMessage(content=prompt)])
            response_text = response.content.strip()

            # 提取 JSON（可能被 markdown code block 包裹）
            json_text = self._extract_json(response_text)
            extracted = json.loads(json_text)

            # Schema 校验：确保所有必要字段存在
            if self.output_schema:
                extracted = self._apply_schema(extracted)

            # 归约：合并新旧特征
            if existing:
                return self.reducer(existing, extracted)
            else:
                # 无已有特征时，用默认值填充缺失字段
                default = self._default_output()
                default.update(extracted)
                return default

        except Exception as e:
            logger.warning(f"LLM 特征提取失败: {e}")
            return existing or self._default_output()

    def _default_output(self) -> dict[str, Any]:
        """根据 output_schema 生成默认输出

        如果定义了 schema，使用 schema 中的 default 值；
        否则返回空 dict。
        """
        if self.output_schema:
            return {
                field: spec.get("default", "" if spec.get("type") == "str" else [])
                for field, spec in self.output_schema.items()
            }
        return {}

    def _apply_schema(self, extracted: dict[str, Any]) -> dict[str, Any]:
        """根据 output_schema 校验和补全提取结果

        Args:
            extracted: LLM 提取的原始结果

        Returns:
            补全后的结果
        """
        result = {}
        for field, spec in self.output_schema.items():
            value = extracted.get(field)
            if value is not None:
                result[field] = value
            else:
                result[field] = spec.get("default", "" if spec.get("type") == "str" else [])
        # 保留 schema 中未定义但 LLM 返回的额外字段
        for key, value in extracted.items():
            if key not in result:
                result[key] = value
        return result

    @staticmethod
    def _extract_json(text: str) -> str:
        """从 LLM 响应中提取 JSON 文本

        处理可能被 markdown code block 包裹的情况。

        Args:
            text: LLM 响应文本

        Returns:
            提取的 JSON 字符串
        """
        if "```json" in text:
            start = text.index("```json") + 7
            end = text.index("```", start)
            return text[start:end].strip()
        if "```" in text:
            start = text.index("```") + 3
            end = text.index("```", start)
            return text[start:end].strip()
        return text.strip()


def default_user_profile_extractor(llm: BaseChatModel) -> FeatureExtractor:
    """创建默认的用户画像提取器

    使用内置的 prompt 和 schema，适用于大多数用户画像场景。

    Args:
        llm: 用于特征提取的 LLM 实例

    Returns:
        配置好的 FeatureExtractor 实例
    """
    # 用户画像的 output_schema
    user_profile_schema = {
        "preferences": {"type": "str", "default": ""},
        "expertise_level": {"type": "str", "default": ""},
        "common_topics": {"type": "list", "default": []},
        "key_facts": {"type": "list", "default": []},
    }

    return FeatureExtractor(
        llm=llm,
        extraction_prompt=DEFAULT_USER_PROFILE_PROMPT,
        output_schema=user_profile_schema,
        reducer=merge_profile_reducer,
    )
