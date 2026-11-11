"""一次 Agent 运行的固定上下文与可变进度。

工具只能从上下文中拿到固定的集群、命名空间、预算和 Store；模型不能修改这些权限。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiops.actions.executor import ActionExecutor
from aiops.agent.budget import BudgetLedger
from aiops.contracts import Incident, TimeRange
from aiops.db import Store
from aiops.investigation.toolbox import Toolbox


@dataclass
class Progress:
    """跨工具共享的进度：新观测、假设变更、动作状态变化都算进展。"""

    activity: int = 0
    last_event: str = ""
    recovered: bool = False
    runbook_ids: list[str] = field(default_factory=list)
    action_state: dict[str, Any] = field(default_factory=dict)
    prediction: dict[str, Any] = field(default_factory=dict)
    needs_human: list[str] = field(default_factory=list)
    seen_activity: int = -1
    budget_nudged: bool = False
    # 本期同一任务按批内顺序串行执行工具，不为跨任务或跨批次引入并行调度。
    tool_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def record(self, event: str) -> None:
        self.activity += 1
        self.last_event = event


@dataclass(frozen=True)
class AgentContext:
    store: Store
    incident: Incident
    investigation_id: str
    window: TimeRange
    budget: BudgetLedger
    toolbox: Toolbox
    executor: ActionExecutor | None
    runtime_path: Path
    max_observation_chars: int
    tool_timeout_seconds: float
    model_reserve_seconds: float
    progress: Progress

    @property
    def cluster(self) -> str:
        return self.incident.cluster

    @property
    def namespace(self) -> str:
        return self.incident.namespace
