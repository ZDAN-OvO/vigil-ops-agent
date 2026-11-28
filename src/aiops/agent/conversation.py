"""CLI 与本地服务共用的会话驱动。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from langchain_core.language_models import BaseChatModel

from aiops.actions.executor import ActionExecutor
from aiops.agent.engine import LangChainInvestigation
from aiops.agent.inspection import InspectionReviewer, SubmitInspectionReview
from aiops.agent.session_runtime import AwaitingActionReceipt, CardActions, RunScope, redact_text
from aiops.config import Settings
from aiops.contracts import (
    Budget,
    Incident,
    Investigation,
    InvestigationStatus,
    SkillTrigger,
    SkillUsage,
    TimeRange,
)
from aiops.db import Store
from aiops.inspection.models import Observation
from aiops.inspection.runtime import InspectionRound
from aiops.investigation.toolbox import ToolboxProtocol
from aiops.sessions.checkpoint import SessionCheckpointer
from aiops.sessions.models import HEARTBEAT_SECONDS, Lease, LeaseLost, SessionEvent, Turn, TurnLane
from aiops.sessions.store import SessionStore


class ExecutorFactory(Protocol):
    def __call__(
        self, *, lease: Lease, turn_id: str
    ) -> AbstractAsyncContextManager[ActionExecutor | None]: ...


@dataclass(frozen=True)
class ConversationResult:
    result: SessionEvent
    card: SessionEvent | None = None

    @property
    def exit_code(self) -> int:
        return self.result.payload["exit_code"]


class ConversationDriver:
    def __init__(
        self,
        *,
        settings: Settings,
        store: Store,
        toolbox: ToolboxProtocol,
        executor_factory: ExecutorFactory | None = None,
        model: BaseChatModel | None = None,
        project_text: Callable[[str], str] = redact_text,
    ) -> None:
        self.settings = settings
        self.store = store
        self.sessions = SessionStore(store)
        self.toolbox = toolbox
        self.executor_factory = executor_factory
        self.model = model
        self.project_text = project_text

    async def review_inspection(
        self,
        scope: InspectionRound,
        observations: tuple[Observation, ...],
        candidates: list[dict],
        *,
        timeout: float,
    ) -> tuple[SubmitInspectionReview | None, Turn, str, str | None]:
        """调查只持有巡检主线租约，动作提交由独立的规则入口处理。"""
        turn = await scope.store.enqueue_investigation(scope.run, "核对本轮巡检异常及登记规则")
        lease = await scope.store.claim_investigation(scope.run, f"inspection-agent-{uuid4().hex}")
        if turn is None or lease is None:
            raise LeaseLost(scope.run.id)
        heartbeat = asyncio.create_task(ConversationRun(self, lease).heartbeat())
        try:
            return await self._review_inspection(scope, observations, candidates, timeout, lease)
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            await self.sessions.release_lease(lease)

    async def _review_inspection(
        self, scope, observations, candidates, timeout: float, lease: Lease
    ) -> tuple[SubmitInspectionReview | None, Turn, str, str | None]:
        turn = await self.sessions.start_main_turn(lease)
        if turn is None:
            raise LeaseLost(scope.run.id)
        settings = self.settings
        budget = Budget(
            max_tool_calls=settings.investigation_budget_tool_calls,
            max_model_calls=settings.investigation_budget_model_calls,
            max_wall_clock_seconds=max(1, int(timeout)),
            max_action_seconds=settings.investigation_budget_action_seconds,
        )
        skill = scope.sop.skill
        started = await scope.store.now()
        incident = Incident(
            id=f"inc-{turn.id}",
            environment=settings.environment,
            cluster=scope.run.cluster,
            namespace=settings.namespace,
            service="",
            trigger="scheduled",
            symptoms="本轮巡检发现异常",
            time_range=TimeRange(
                start=min(scope.run.scheduled_at, started - timedelta(seconds=1)), end=started
            ),
            context=[scope.run.id, scope.run.sop_hash],
        )
        investigation = Investigation(
            id=f"inv-{turn.id}",
            incident_id=incident.id,
            status=InvestigationStatus.RUNNING,
            model=settings.model_name or (self.model._llm_type if self.model else "unavailable"),
            policy_version=scope.run.sop_hash,
            budget=budget,
            started_at=started,
            skills=[
                SkillUsage(
                    name=skill.name, content_hash=skill.content_hash, trigger=SkillTrigger.SIGNAL
                )
            ],
        )
        async with self.sessions.run(lease, turn.id) as tx:
            await scope.store.require_active(tx.conn, scope.run)
            await tx.ledger.save_incident(incident)
            await tx.ledger.create_investigation(investigation)
        review = reviewer = None
        reason = "model_unavailable"
        if timeout <= 0:
            reason = "insufficient_investigation_budget"
        elif self.model is not None or settings.model_name:
            try:
                async with asyncio.timeout(timeout):
                    model = self.model or LangChainInvestigation.build_model(settings)
                    reviewer = InspectionReviewer(model, scope, budget)
                    review = await reviewer.review(observations, candidates)
                reason = None if review is not None else "invalid_review"
            except LeaseLost:
                raise
            except TimeoutError:
                reason = "investigation_budget_exhausted"
            except Exception:
                reason = "investigation_failed"
        summary = self.project_text(review.explanation) if review else "巡检核对未完成，需人工核对"
        async with self.sessions.run(lease, turn.id) as tx:
            _, finished = await scope.store.require_active(tx.conn, scope.run)
            if reviewer is not None:
                await tx.ledger.add_usage(
                    investigation.id,
                    tool_calls=reviewer.tool_calls,
                    model_calls=reviewer.model_calls,
                )
            await tx.ledger.set_investigation_status(
                investigation.id,
                InvestigationStatus.COMPLETED if review else InvestigationStatus.INCONCLUSIVE,
                conclusion=summary if review else None,
                termination_reason=reason,
                finished_at=finished,
            )
            await tx.append_event("message", {"role": "assistant", "text": summary, "lane": "main"})
            await tx.append_event(
                "result",
                {
                    "schema_version": 1,
                    "status": "completed",
                    "summary": summary,
                    "exit_code": 0,
                    "investigation_id": investigation.id,
                    "termination_code": reason or "inspection_reviewed",
                    "lane": "main",
                },
            )
            await tx.set_turn_state("completed")
        return review, turn, investigation.id, reason

    @asynccontextmanager
    async def claim(
        self, session_id: str, *, lane: TurnLane = "main"
    ) -> AsyncIterator[ConversationRun | None]:
        """领取单独的驱动权。在等待人工决定时可保持上下文并继续心跳。"""
        owner = f"conversation-{uuid4().hex}"
        if lane == "main":
            lease = await self.sessions.acquire_main_lease(session_id, owner)
        elif lane == "side":
            lease = await self.sessions.acquire_side_lease(session_id, owner)
        else:
            raise ValueError("轮次必须为 main 或 side")
        if lease is None:
            yield None
            return
        run = ConversationRun(self, lease)
        heartbeat = asyncio.create_task(run.heartbeat())
        try:
            yield run
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            await self.sessions.release_lease(lease)


class ConversationRun:
    def __init__(self, driver: ConversationDriver, lease: Lease) -> None:
        self._driver = driver
        self.lease = lease
        self._heartbeat_error: Exception | None = None
        self._drive_lock = asyncio.Lock()

    async def heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                self.lease = await self._driver.sessions.renew_lease(self.lease)
            except Exception as exc:
                # 正在执行的动作自行收尾。此后模型、工具和所有调查提交均检查租约。
                self._heartbeat_error = exc
                return

    def _check_heartbeat(self) -> None:
        if self._heartbeat_error is not None:
            raise LeaseLost(self.lease.session_id) from self._heartbeat_error

    async def drive(self) -> ConversationResult | None:
        """驱动一个轮次；主线停在卡片上时可再次调用以处理数据库里的决定。"""
        async with self._drive_lock:
            self._check_heartbeat()
            driver = self._driver
            if self.lease.side_turn_id:
                turn = await driver.sessions.get_turn(self.lease.side_turn_id)
                if turn is None or turn.finished_at is not None:
                    return None
            else:
                turn = await driver.sessions.start_main_turn(self.lease)
                if turn is None:
                    return None
            scope = RunScope(
                driver.sessions, self.lease, turn, self._check_heartbeat, driver.project_text
            )
            if driver.settings.model_name is None and driver.model is None:
                # 规则后台仍可处置结构化告警，模型调查要等模型可用。
                return None
            incident, investigation = await self._investigation(scope, turn)
            async with self._open_executor(scope) as executor:
                engine = LangChainInvestigation(
                    settings=driver.settings,
                    store=driver.store.for_turn(self.lease, turn.id),
                    dsn="",
                    incident=incident,
                    investigation=investigation,
                    toolbox=driver.toolbox,
                    executor=executor,
                    model=driver.model,
                    scope=scope,
                )
                result, card = await engine.run_conversation(
                    SessionCheckpointer(driver.store, self.lease, turn)
                )
            return ConversationResult(result, card)

    @asynccontextmanager
    async def _open_executor(self, scope: RunScope) -> AsyncIterator[ActionExecutor | None]:
        factory = self._driver.executor_factory
        if scope.turn.lane == "side" or factory is None:
            yield None
            return
        await scope.ensure_active()
        async with factory(lease=scope.lease, turn_id=scope.turn.id) as executor:
            yield executor if isinstance(executor, CardActions) else None

    async def _investigation(self, scope: RunScope, turn: Turn) -> tuple[Incident, Investigation]:
        driver = self._driver
        investigation_id = await driver.store.get_investigation_id_for_turn(turn.id)
        if investigation_id is not None:
            investigation = await driver.store.get_investigation(investigation_id)
            if investigation is None:
                raise LookupError("轮次关联的调查不存在")
            incident = await driver.store.get_incident(investigation.incident_id)
            if incident is None:
                raise LookupError("调查关联的事件不存在")
            return incident, investigation

        session = await driver.sessions.get_session(turn.session_id)
        first = await driver.sessions.get_input(turn.first_input_id)
        if session is None or first is None:
            raise LookupError("会话或轮次首条输入不存在")
        settings = driver.settings
        if settings.model_name is None:
            raise ValueError("未配置 VIGIL_MODEL_NAME")
        now = datetime.now(UTC)
        from_alert = session.source == "alert" and turn.lane == "main" and first.queued_seq == 2
        incident = Incident(
            id=f"inc-{turn.id}",
            environment=settings.environment,
            cluster=settings.cluster,
            namespace=settings.namespace,
            service="",
            symptoms=first.text,
            trigger="alert" if from_alert else "manual",
            alert_names=first.alert_names if from_alert else [],
            time_range=TimeRange(start=now - timedelta(minutes=15), end=now),
        )
        investigation = Investigation(
            id=f"inv-{turn.id}",
            incident_id=incident.id,
            status=InvestigationStatus.RUNNING,
            model=settings.model_name,
            policy_version="conversation",
            budget=Budget(
                max_tool_calls=settings.investigation_budget_tool_calls,
                max_model_calls=settings.investigation_budget_model_calls,
                max_wall_clock_seconds=settings.investigation_budget_seconds,
                max_action_seconds=settings.investigation_budget_action_seconds,
            ),
            started_at=now,
        )
        async with scope.transaction() as tx:
            await tx.ledger.save_incident(incident)
            await tx.ledger.create_investigation(investigation)
        return incident, investigation


__all__ = ["AwaitingActionReceipt", "ConversationDriver", "ConversationResult", "redact_text"]
