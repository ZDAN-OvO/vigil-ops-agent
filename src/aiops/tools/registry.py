"""工具注册表：按提供方装配，并按只读边界选择子 Agent 可用的工具。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from langchain_core.tools import BaseTool

from aiops.agent.tools import (
    assess_evidence,
    knowledge_get_runbook,
    knowledge_list_runbooks,
    kubernetes_list_deployments,
    kubernetes_list_events,
    kubernetes_list_logs,
    kubernetes_list_pods,
    kubernetes_list_services,
    manage_hypotheses,
    prometheus_query_range,
    request_action,
)

SUPPORTED_PROVIDERS = frozenset({"kubernetes", "prometheus"})


class ToolRegistrationError(RuntimeError):
    """工具声明重复、缺少提供方或只读约束被破坏。"""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    tool: BaseTool | Callable
    read_only: bool
    providers: tuple[str, ...] = ()
    input_model: str = ""
    output_model: str = "str"


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("prometheus_query_range", prometheus_query_range, True, ("prometheus",)),
    ToolSpec("kubernetes_list_deployments", kubernetes_list_deployments, True, ("kubernetes",)),
    ToolSpec("kubernetes_list_pods", kubernetes_list_pods, True, ("kubernetes",)),
    ToolSpec("kubernetes_list_events", kubernetes_list_events, True, ("kubernetes",)),
    ToolSpec("kubernetes_list_services", kubernetes_list_services, True, ("kubernetes",)),
    ToolSpec("kubernetes_list_logs", kubernetes_list_logs, True, ("kubernetes",)),
    ToolSpec("knowledge_list_runbooks", knowledge_list_runbooks, True),
    ToolSpec("knowledge_get_runbook", knowledge_get_runbook, True),
    ToolSpec("manage_hypotheses", manage_hypotheses, False),
    ToolSpec("assess_evidence", assess_evidence, False),
    ToolSpec("request_action", request_action, False, ("kubernetes",)),
)


def tool_name(tool: BaseTool | Callable) -> str:
    return getattr(tool, "name", getattr(tool, "__name__", str(tool)))


def build_registry() -> list[ToolSpec]:
    names = [spec.name for spec in TOOL_SPECS]
    if len(names) != len(set(names)):
        raise ToolRegistrationError("工具名称重复")
    for spec in TOOL_SPECS:
        if tool_name(spec.tool) != spec.name:
            raise ToolRegistrationError(f"工具名不一致：{spec.name}")
        unknown = set(spec.providers) - SUPPORTED_PROVIDERS
        if unknown:
            raise ToolRegistrationError(f"未知提供方：{sorted(unknown)}")
    enriched: list[ToolSpec] = []
    for spec in TOOL_SPECS:
        schema = getattr(spec.tool, "args_schema", None)
        enriched.append(
            replace(
                spec,
                input_model=getattr(schema, "__name__", "") or spec.name,
                output_model="str",
            )
        )
    return enriched


def select_tools(
    registry: Sequence[ToolSpec], *, read_only: bool | None = None
) -> list[BaseTool | Callable]:
    return [spec.tool for spec in registry if read_only is None or spec.read_only is read_only]


def read_only_names(registry: Sequence[ToolSpec]) -> set[str]:
    return {spec.name for spec in registry if spec.read_only}


__all__ = [
    "SUPPORTED_PROVIDERS",
    "TOOL_SPECS",
    "ToolRegistrationError",
    "ToolSpec",
    "build_registry",
    "read_only_names",
    "select_tools",
    "tool_name",
]
