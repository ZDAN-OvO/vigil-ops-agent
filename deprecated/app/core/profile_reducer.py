"""用户画像归约逻辑（Reducer）

参考 LangGraph 的 reducer 设计模式（如 add_messages），
定义画像合并的协议和默认实现。

Reducer 函数签名：(existing, extracted) -> merged
与 LangGraph 的 Annotated[field, reducer] 模式一致。

使用方式：
    from app.core.profile_reducer import merge_profile_reducer

    merged = merge_profile_reducer(existing_profile, extracted_profile)
"""

from collections.abc import Callable
from typing import Any

# Reducer 类型：接收 (已有画像, 新提取画像)，返回合并后的画像
ProfileReducer = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


def merge_profile_reducer(
    existing: dict[str, Any],
    extracted: dict[str, Any],
) -> dict[str, Any]:
    """默认画像合并策略

    合并规则：
    - 字符串字段（preferences, expertise_level）：新值覆盖旧值（非空时）
    - 列表字段（common_topics, key_facts）：追加后去重，保留顺序
    - 其他字段：新值覆盖旧值（非空时）

    Args:
        existing: 已有画像
        extracted: LLM 新提取的画像

    Returns:
        合并后的画像
    """
    result = {**existing}

    for key, new_value in extracted.items():
        if not new_value and new_value != 0:
            # 空值不覆盖（保留旧值）
            continue

        old_value = result.get(key)

        if isinstance(old_value, list) and isinstance(new_value, list):
            # 列表字段：追加后去重，保留顺序
            merged = list(dict.fromkeys(old_value + new_value))
            result[key] = merged
        else:
            # 字符串/其他字段：新值覆盖旧值
            result[key] = new_value

    return result


def replace_profile_reducer(
    _existing: dict[str, Any],
    extracted: dict[str, Any],
) -> dict[str, Any]:
    """完全替换策略（不做合并，直接用新值替换）

    适用于需要完全重置画像的场景。

    Args:
        _existing: 已有画像（忽略）
        extracted: LLM 新提取的画像

    Returns:
        新画像（原样返回）
    """
    return {**extracted}
