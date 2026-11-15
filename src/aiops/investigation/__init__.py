"""调查能力：只读工具分发与装配。

单 Agent 循环位于 `aiops.agent`；这里保留共用的工具箱接口与知识工具。
"""

from aiops.investigation.toolbox import QueryRequest, Toolbox, ToolboxProtocol

__all__ = ["QueryRequest", "Toolbox", "ToolboxProtocol"]
