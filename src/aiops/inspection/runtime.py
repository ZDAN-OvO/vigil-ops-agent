"""巡检采集、处理钩子和 serve 后台调度。"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import uuid4

from aiops.inspection.config import InspectionSOP
from aiops.inspection.models import Check, InspectionRun, Observation, Sample
from aiops.inspection.store import InspectionStore
from aiops.sessions.models import HEARTBEAT_SECONDS, LeaseLost

_LOG = logging.getLogger(__name__)
SampleCheck = Callable[[Check, str, float], Awaitable[Sample]]


@dataclass(frozen=True)
class InspectionRound:
    run: InspectionRun
    store: InspectionStore
    sop: InspectionSOP
    _sample: SampleCheck

    async def sample(self, check: Check, *, initial: bool) -> Observation:
        check, remaining = await self.store.registered_check(self.run, check)
        try:
            async with asyncio.timeout(remaining):
                sample = await self._sample(check, self.run.id, remaining)
        except TimeoutError:
            sample = Sample.insufficient("timeout")
        except Exception as exc:
            _LOG.warning("巡检采集失败（%s，%s）", check.check_id, type(exc).__name__)
            sample = Sample.insufficient("collection_failed")
        return await self.store.save_observation(self.run, check, sample, initial=initial)

    async def resample(self, check: Check) -> Observation:
        """只允许本轮登记检查，补采追加独立证据并刷新摘要。"""
        return await self.sample(check, initial=False)


InspectionHook = Callable[[InspectionRound, tuple[Observation, ...]], Awaitable[None]]


@dataclass(frozen=True)
class InspectionScheduler:
    store: InspectionStore
    cluster: str
    interval_seconds: int
    timeout_seconds: int
    sop: InspectionSOP
    checks: tuple[Check, ...]
    sample: SampleCheck
    on_observations: InspectionHook | None = None

    async def execute(self, run: InspectionRun) -> None:
        scope = InspectionRound(run, self.store, self.sop, self.sample)
        try:
            async with asyncio.timeout(await self.store.remaining(run)):
                observations = []
                for check in run.checks:
                    observations.append(await scope.sample(check, initial=True))
                await self.store.publish_summary(run)
                if self.on_observations is not None:
                    await self.on_observations(scope, tuple(observations))
        except (TimeoutError, LeaseLost):
            await self.store.finish(run, status="timed_out", reason="deadline_exceeded")
        except asyncio.CancelledError:
            await self.store.finish(run, status="interrupted", reason="stopped")
            raise
        except Exception as exc:
            _LOG.error("巡检轮次失败（%s）", type(exc).__name__)
            await self.store.finish(run, status="failed", reason="execution_failed")
        else:
            await self.store.finish(run)

    async def drive(self, stop: asyncio.Event, *, poll_seconds: float = 1) -> None:
        owner = uuid4().hex
        started_at = None
        running: tuple[InspectionRun, asyncio.Task[None]] | None = None
        loop = asyncio.get_running_loop()
        heartbeat_at = loop.time()
        try:
            while not stop.is_set():
                try:
                    if started_at is None:
                        started_at = await self.store.prepare(self.cluster, self.interval_seconds)
                    if running is not None:
                        run, task = running
                        if task.done():
                            running = None
                            if not task.cancelled():
                                task.result()
                        else:
                            try:
                                if loop.time() >= heartbeat_at:
                                    await self.store.renew(run)
                                    heartbeat_at = loop.time() + HEARTBEAT_SECONDS
                                else:
                                    await self.store.remaining(run)
                            except LeaseLost:
                                task.cancel()
                    claimed = await self.store.claim_due(
                        cluster=self.cluster,
                        started_at=started_at,
                        interval_seconds=self.interval_seconds,
                        timeout_seconds=self.timeout_seconds,
                        owner=owner,
                        sop_hash=self.sop.skill.content_hash,
                        checks=self.checks,
                    )
                    if claimed is not None:
                        if running is not None:
                            running[1].cancel()
                            await asyncio.gather(running[1], return_exceptions=True)
                        running = claimed, asyncio.create_task(self.execute(claimed))
                        heartbeat_at = loop.time() + HEARTBEAT_SECONDS
                except Exception as exc:
                    _LOG.error("巡检调度失败，稍后重试（%s）", type(exc).__name__)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
                except TimeoutError:
                    pass
        finally:
            if running is not None:
                run, task = running
                task.cancel()
                try:
                    await self.store.finish(run, status="interrupted", reason="stopped")
                finally:
                    await asyncio.gather(task, return_exceptions=True)
