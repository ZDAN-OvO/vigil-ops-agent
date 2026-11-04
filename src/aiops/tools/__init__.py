"""只读工具适配器；不提供任何集群写操作。"""

from aiops.tools.base import RawRef, ToolContext

__all__ = ["RawRef", "ToolContext"]
