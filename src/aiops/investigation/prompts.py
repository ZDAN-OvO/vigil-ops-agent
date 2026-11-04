"""调查循环的提示词；只放事件与证据，不放场景真值、脚本或文件路径。"""

from __future__ import annotations

import json
from typing import Any

from aiops.investigation.schemas import Assessment, PlanRequest

SYSTEM_PROMPT = """你是 Kubernetes 故障调查助手，只做只读调查。

规则：
1. 只能使用给定的只读工具，绝不提出写操作。
2. 假设必须能被观测区分；给出你期望看到的证据。
3. 证据不足或查询失败时如实说明，不要补造数据。
4. 结论只覆盖已有证据能支持的范围；不确定就用 inconclusive。
5. 只有证据足够时才结束，结束必须给出结论或说明信息不足。
"""

TOOL_GUIDE = """可用工具：
- prometheus.query_range：需要 expression、lookback_seconds（60 到 3600）、
  step_seconds（5 到 300）。
- kubernetes.list_deployments：可选 selector。
- kubernetes.list_pods：可选 selector。
- kubernetes.list_events：可选 involved_name，留空表示全部事件。
未用到的字段填空字符串或 0。
"""


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def plan_messages(incident: dict[str, Any], remaining: dict[str, int]) -> list[dict[str, str]]:
    schema = PlanRequest.model_json_schema()
    user = (
        f"事件：\n{_dump(incident)}\n\n"
        f"{TOOL_GUIDE}\n"
        f"剩余预算：{_dump(remaining)}\n\n"
        "请提出 1 到 3 个候选假设，并给出第一批查询。\n"
        f"输出必须匹配这个 schema：{_dump(schema)}"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def assess_messages(
    incident: dict[str, Any],
    hypotheses: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    remaining: dict[str, int],
) -> list[dict[str, str]]:
    schema = Assessment.model_json_schema()
    user = (
        f"事件：\n{_dump(incident)}\n\n"
        f"当前假设（按列表下标引用）：\n{_dump(hypotheses)}\n\n"
        f"已有证据（按列表下标引用）：\n{_dump(evidence)}\n\n"
        f"失败的查询：\n{_dump(failures)}\n\n"
        f"剩余预算：{_dump(remaining)}\n\n"
        "请更新假设状态、标注每条证据的支持关系，然后给出下一批查询或结束。\n"
        f"输出必须匹配这个 schema：{_dump(schema)}"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


__all__ = ["SYSTEM_PROMPT", "TOOL_GUIDE", "assess_messages", "plan_messages"]
