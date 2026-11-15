"""调查能力：只读工具分发、固定规则基线与装配。

单 Agent 循环位于 `aiops.agent`；这里保留共用的工具箱基线与知识工具。
"""

from aiops.investigation.toolbox import QueryRequest, Toolbox, ToolboxProtocol

__all__ = ["QueryRequest", "Toolbox", "ToolboxProtocol"]
