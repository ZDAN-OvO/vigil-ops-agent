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
6. 只有在证据指向近期发布变更时才建议动作：action_kind 填 rollout_undo，
   并在 action_deployment 写出 Deployment 名称；否则 action_kind 填 none，
   另外两个字段留空字符串。action_evidence 必须列出支撑该动作的证据下标
   （按 evidence 列表下标），建议动作时不能为空。动作仍需规则判定和人工
   审批，不会自动执行。
7. 容器反复 OOMKilled 或 restartCount 持续上升时，比较 Deployment 当前模板与
   历史 revision 的资源规格。只重建 Pod 会沿用当前模板，不能修复规格不足；
   只有存在可回退的历史模板且证据支持时才建议 rollout_undo。
8. 不要建议直接修改内存限额、删除 Pod 或重启工作负载；action_kind 只有
   rollout_undo 和 none 两种。
9. 入口服务的告警不等于入口自身故障。入口 Pod 与 Deployment 健康时，必须列出
   命名空间内全部 Pod 和 Deployment，检查依赖服务的重启、OOM 和资源规格变化。
10. 延迟或错误出现在依赖链路而服务端没有对应证据时，说明证据边界，不要把
   服务端内部原因当作已证实结论。
11. 如果根因是内存限额或其他资源规格不足，必须在 conclusion 中说明：重建 Pod
   会沿用当前 Deployment 模板，无法修复该故障，因此不建议重建。
"""

TOOL_GUIDE = """可用工具：
- prometheus.query_range：需要 expression、lookback_seconds（60 到 3600）、
  step_seconds（5 到 300）。
- kubernetes.list_deployments：可选 selector，匹配 Deployment 标签或其 Pod 选择器；
  返回当前容器资源规格和按 revision 的历史模板。
- kubernetes.list_pods：可选 selector；返回重启计数、last_terminated 和容器资源规格。
- kubernetes.list_events：可选 involved_name，留空表示全部事件。
selector 是 Kubernetes 标签选择器（如 app=frontend）；留空表示命名空间内全部对象，
不要把命名空间名填进 selector。未用到的字段填空字符串或 0。
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
