"""分阶段预算：调查与动作各自累计，暂停和进程未运行时间不计入有效运行时间。

预算同时落在 `investigation_tasks`：同一任务跨进程恢复时用量不清零。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from aiops.contracts import Budget, InvestigationTask, TaskPhase
from aiops.db import Store


class BudgetExhausted(RuntimeError):
    """预算不足；调用方按终止原因结束，不得继续发请求。"""

    def __init__(self, item: str, detail: str = "") -> None:
        super().__init__(detail or f"预算耗尽：{item}")
        self.item = item


class WriteReserveUnavailable(RuntimeError):
    """动作开始前无法预留完整适配器时间或一次反馈模型调用。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)


PHASE_INVESTIGATION = "investigation"
PHASE_ACTION = "action"
PHASE_IDLE = "idle"


@dataclass
class BudgetLedger:
    store: Store
    investigation_id: str
    budget: Budget
    task: InvestigationTask
    clock: Callable[[], float] = time.monotonic
    phase: str = field(default=PHASE_IDLE)
    _segment_started: float = field(default=0.0)
    _investigation_base: float = 0.0
    _action_base: float = 0.0
    used_model_calls: int = 0
    used_tool_calls: int = 0
    no_progress_rounds: int = 0

    @classmethod
    def restore(
        cls,
        *,
        store: Store,
        investigation_id: str,
        budget: Budget,
        task: InvestigationTask,
        clock: Callable[[], float] = time.monotonic,
    ) -> BudgetLedger:
        return cls(
            store=store,
            investigation_id=investigation_id,
            budget=budget,
            task=task,
            clock=clock,
            _investigation_base=task.investigation_seconds,
            _action_base=task.action_seconds,
            used_model_calls=task.used_model_calls,
            used_tool_calls=task.used_tool_calls,
            no_progress_rounds=task.no_progress_rounds,
        )

    # ---- 有效运行时间 ----

    def _tick(self) -> float:
        if self.phase == PHASE_IDLE:
            return 0.0
        return max(0.0, self.clock() - self._segment_started)

    @property
    def investigation_seconds(self) -> float:
        return self._investigation_base + (
            self._tick() if self.phase == PHASE_INVESTIGATION else 0.0
        )

    @property
    def action_seconds(self) -> float:
        return self._action_base + (self._tick() if self.phase == PHASE_ACTION else 0.0)

    @property
    def total_seconds(self) -> float:
        return self.investigation_seconds + self.action_seconds

    def remaining(self) -> dict[str, float | int]:
        return {
            "tool_calls": max(0, self.budget.max_tool_calls - self.used_tool_calls),
            "model_calls": max(0, self.budget.max_model_calls - self.used_model_calls),
            "investigation_seconds": round(
                max(0.0, self.budget.max_wall_clock_seconds - self.investigation_seconds), 1
            ),
            "action_seconds": round(
                max(0.0, self.budget.max_action_seconds - self.action_seconds), 1
            ),
            "total_seconds": round(max(0.0, self.budget.max_total_seconds - self.total_seconds), 1),
        }

    # ---- 阶段切换与落库 ----

    async def start(self) -> None:
        """开始或恢复一段有效运行；此前的时间不计入。"""
        self._segment_started = self.clock()
        self.phase = PHASE_INVESTIGATION
        await self.persist()

    async def pause(self) -> None:
        """暂停（等待审批或进程退出）：把已用秒数落库，之后不计时。"""
        self._flush()
        self.phase = PHASE_IDLE
        await self.persist()

    def _flush(self) -> None:
        if self.phase == PHASE_INVESTIGATION:
            self._investigation_base += self._tick()
        elif self.phase == PHASE_ACTION:
            self._action_base += self._tick()
        self._segment_started = self.clock()

    async def persist(self) -> None:
        await self.store.update_task_usage(
            self.investigation_id,
            used_model_calls=self.used_model_calls,
            used_tool_calls=self.used_tool_calls,
            investigation_seconds=round(self.investigation_seconds, 3),
            action_seconds=round(self.action_seconds, 3),
            no_progress_rounds=self.no_progress_rounds,
        )

    # ---- 请求计数 ----

    def check_model_budget(self) -> None:
        if self.used_model_calls >= self.budget.max_model_calls:
            raise BudgetExhausted("model_calls", "模型调用次数已用完")
        if self.investigation_seconds >= self.budget.max_wall_clock_seconds:
            raise BudgetExhausted("investigation_seconds", "调查有效运行时间已用完")
        if self.total_seconds >= self.budget.max_total_seconds:
            raise BudgetExhausted("total_seconds", "总有效运行时间已用完")

    async def spend_model_call(self) -> None:
        self.check_model_budget()
        self.used_model_calls += 1
        await self.persist()

    def check_tool_budget(self) -> None:
        if self.used_tool_calls >= self.budget.max_tool_calls:
            raise BudgetExhausted("tool_calls", "业务工具请求次数已用完")
        if self.investigation_seconds >= self.budget.max_wall_clock_seconds:
            raise BudgetExhausted("investigation_seconds", "调查有效运行时间已用完")
        if self.total_seconds >= self.budget.max_total_seconds:
            raise BudgetExhausted("total_seconds", "总有效运行时间已用完")

    async def spend_tool_call(self) -> None:
        self.check_tool_budget()
        self.used_tool_calls += 1
        await self.persist()

    # ---- 动作预留 ----

    def model_call_timeout(self, *, configured: float) -> float:
        """单次模型调用的上限：不越过配置上限，也不越过剩余调查时间。"""
        remaining = max(0.5, self.budget.max_wall_clock_seconds - self.investigation_seconds)
        return max(0.5, min(configured, remaining))

    def read_tool_timeout(self, *, configured: float) -> float:
        remaining = max(0.5, self.budget.max_wall_clock_seconds - self.investigation_seconds)
        return max(0.5, min(configured, remaining))

    def check_write_reserve(self, *, adapter_bound: float, model_reserve: float) -> None:
        """动作开始前必须预留完整适配器上界和至少一次反馈模型调用。"""
        action_left = self.budget.max_action_seconds - self.action_seconds
        if action_left < adapter_bound:
            raise WriteReserveUnavailable(
                f"动作预算不足：剩余 {action_left:.0f} 秒，适配器上界 {adapter_bound:.0f} 秒"
            )
        model_left = self.remaining()["model_calls"]
        if model_left < 1:
            raise WriteReserveUnavailable("模型调用预算不足：至少需要保留 1 次反馈模型调用")
        investigation_left = self.budget.max_wall_clock_seconds - self.investigation_seconds
        if investigation_left < model_reserve:
            raise WriteReserveUnavailable(
                f"调查预算不足：剩余 {investigation_left:.0f} 秒，"
                f"需要保留 {model_reserve:.0f} 秒执行至少一次反馈模型调用"
            )
        total_left = self.budget.max_total_seconds - self.total_seconds
        if total_left < adapter_bound + model_reserve:
            raise WriteReserveUnavailable(
                f"总预算不足：剩余 {total_left:.0f} 秒，需要 {adapter_bound + model_reserve:.0f} 秒"
            )

    async def begin_action(self) -> None:
        self._flush()
        self.phase = PHASE_ACTION
        self._segment_started = self.clock()
        await self.persist()

    async def end_action(self) -> None:
        self._flush()
        self.phase = PHASE_INVESTIGATION
        self._segment_started = self.clock()
        await self.persist()

    # ---- 无进展 ----

    async def bump_no_progress(self) -> int:
        self.no_progress_rounds += 1
        await self.persist()
        return self.no_progress_rounds

    async def reset_no_progress(self) -> None:
        if self.no_progress_rounds:
            self.no_progress_rounds = 0
            await self.persist()

    def terminal_phase(self) -> bool:
        return self.task.phase is TaskPhase.TERMINAL

    def calendar_seconds(self) -> float:
        return max(0.0, (datetime.now(UTC) - self.task.calendar_started_at).total_seconds())
