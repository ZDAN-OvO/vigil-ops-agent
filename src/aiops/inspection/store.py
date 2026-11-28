"""数据库裁决巡检时点、轮次所有权及证据提交。"""

from dataclasses import replace
from datetime import datetime, timedelta
from uuid import uuid4

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from aiops.db import Store
from aiops.inspection.models import (
    Check,
    InspectionClock,
    InspectionRun,
    Observation,
    RunStatus,
    Sample,
)
from aiops.sessions.models import LEASE_SECONDS, Lease, LeaseLost, Turn
from aiops.sessions.projection import project_payload, project_text
from aiops.sessions.store import SessionStore, _one


def _run(row: dict) -> InspectionRun:
    return InspectionRun(**{**row, "checks": tuple(Check.model_validate(i) for i in row["checks"])})


class InspectionStore:
    def __init__(self, store: Store, *, clock: InspectionClock | None = None) -> None:
        self._store = store
        self._clock = clock
        self.sessions = SessionStore(store)

    async def now(self, conn: AsyncConnection | None = None) -> datetime:
        if self._clock is not None:
            return self._clock()
        if conn is not None:
            return (await _one(conn, "SELECT clock_timestamp() AS now"))["now"]
        async with self._store.transaction() as conn:
            return await self.now(conn)

    async def prepare(self, cluster: str, interval_seconds: int) -> datetime:
        async with self._store.transaction() as conn:
            now = await self.now(conn)
            await conn.execute(
                "INSERT INTO inspection_schedules (cluster, next_at) VALUES (%s, %s) "
                "ON CONFLICT (cluster) DO NOTHING",
                (cluster, now + timedelta(seconds=interval_seconds)),
            )
            return now

    async def claim_due(
        self,
        *,
        cluster: str,
        started_at: datetime,
        interval_seconds: int,
        timeout_seconds: int,
        owner: str,
        sop_hash: str,
        checks: tuple[Check, ...],
    ) -> InspectionRun | None:
        """同一集群串行结算失联轮次、跳过繁忙时点并创建新会话。"""
        interval = timedelta(seconds=interval_seconds)
        async with self._store.transaction() as conn:
            schedule = await _one(
                conn,
                "SELECT next_at FROM inspection_schedules WHERE cluster = %s FOR UPDATE",
                (cluster,),
            )
            if schedule is None:
                return None
            latest = await _one(
                conn,
                "SELECT * FROM inspection_runs WHERE cluster = %s "
                "ORDER BY scheduled_at DESC LIMIT 1",
                (cluster,),
            )
            current = _run(latest) if latest is not None else None
            if current is not None and current.status == "running":
                current = await self._lock(conn, current.id, current.session_id)
            now = await self.now(conn)
            busy = current is not None and current.status == "running"
            if busy and (expired := self._expired(current, now)) is not None:
                current = await self._finish(conn, current, now, *expired)
                busy = False
            next_at = schedule["next_at"]
            if next_at > now:
                return None
            scheduled_at = next_at + (now - next_at) // interval * interval
            if scheduled_at < started_at:
                # 刚启动的副本不补跑，其他仍在线的副本可以领取原时点。
                return None
            await conn.execute(
                "UPDATE inspection_schedules SET next_at = %s WHERE cluster = %s",
                (scheduled_at + interval, cluster),
            )
            # 定时器即便延迟唤醒，也不能补做当时仍有活动轮次的时点。
            if busy or (current is not None and scheduled_at < (current.finished_at or now)):
                return None
            session = await self.sessions.create_inspection_session(
                conn, title=project_text(f"巡检 {cluster} · {scheduled_at.isoformat()}")
            )
            row = await _one(
                conn,
                """
                INSERT INTO inspection_runs (id, session_id, cluster, scheduled_at, deadline,
                                             owner, lease_expires_at, sop_hash, status, checks)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'running', %s) RETURNING *
                """,
                (
                    uuid4().hex,
                    session.id,
                    cluster,
                    scheduled_at,
                    now + timedelta(seconds=timeout_seconds),
                    owner,
                    now + timedelta(seconds=LEASE_SECONDS),
                    sop_hash,
                    Jsonb([check.model_dump(mode="json") for check in checks]),
                ),
            )
            run = _run(row)
            await self._append_summary(conn, run)
            return run

    @staticmethod
    def _expired(run: InspectionRun, now: datetime) -> tuple[RunStatus, str] | None:
        if now >= run.deadline and run.deadline <= run.lease_expires_at:
            return "timed_out", "deadline_exceeded"
        if now >= run.lease_expires_at:
            return "interrupted", "lease_expired"
        return None

    async def _lock(self, conn: AsyncConnection, run_id: str, session_id: str) -> InspectionRun:
        await self.sessions.lock_session(conn, session_id)
        row = await _one(
            conn,
            "SELECT * FROM inspection_runs WHERE id = %s AND session_id = %s FOR UPDATE",
            (run_id, session_id),
        )
        if row is None:
            raise LeaseLost(run_id)
        return _run(row)

    async def require_active(
        self, conn: AsyncConnection, token: InspectionRun
    ) -> tuple[InspectionRun, datetime]:
        """在写入或授予发送权的事务内调用，锁顺序为会话、巡检轮次。"""
        run = await self._lock(conn, token.id, token.session_id)
        now = await self.now(conn)
        if (
            run.status != "running"
            or run.owner != token.owner
            or run.generation != token.generation
            or self._expired(run, now) is not None
        ):
            raise LeaseLost(run.id)
        return run, now

    async def require_active_for_send(
        self, conn: AsyncConnection, *, run_id: str, session_id: str
    ) -> tuple[InspectionRun, datetime]:
        # 账本引用的轮次 ID 不会转交给其他调度者，崩溃接管只会中断旧轮次。
        current = await self._lock(conn, run_id, session_id)
        now = await self.now(conn)
        if current.status != "running" or self._expired(current, now) is not None:
            raise LeaseLost(run_id)
        return current, now

    async def remaining(self, run: InspectionRun) -> float:
        async with self._store.transaction() as conn:
            current, now = await self.require_active(conn, run)
            return (current.deadline - now).total_seconds()

    async def registered_check(self, run: InspectionRun, check: Check) -> tuple[Check, float]:
        async with self._store.transaction() as conn:
            current, now = await self.require_active(conn, run)
            registered = next((entry for entry in current.checks if entry == check), None)
            if registered is None:
                raise ValueError("检查不属于本轮巡检")
            return registered, (current.deadline - now).total_seconds()

    async def renew(self, run: InspectionRun) -> None:
        async with self._store.transaction() as conn:
            current, now = await self.require_active(conn, run)
            await conn.execute(
                "UPDATE inspection_runs SET lease_expires_at = %s WHERE id = %s",
                (now + timedelta(seconds=LEASE_SECONDS), current.id),
            )

    async def get_run(self, run_id: str) -> InspectionRun | None:
        async with self._store.transaction() as conn:
            row = await _one(conn, "SELECT * FROM inspection_runs WHERE id = %s", (run_id,))
            return _run(row) if row is not None else None

    async def save_observation(
        self, run: InspectionRun, check: Check, sample: Sample, *, initial: bool = True
    ) -> Observation:
        async with self._store.transaction() as conn:
            current, _ = await self.require_active(conn, run)
            if check not in current.checks:
                raise ValueError("检查不属于本轮巡检")
            observation = await self._insert_observation(conn, current, check, sample, initial)
            if not initial:
                await self._append_summary(conn, current)
            return observation

    async def _insert_observation(
        self, conn: AsyncConnection, run: InspectionRun, check: Check, sample: Sample, initial: bool
    ) -> Observation:
        observation_id = uuid4().hex
        observation = Observation(
            observation_id,
            check.ordinal,
            check.check_id,
            check.target,
            sample.status,
            sample.reason,
            sample.sampled_at,
            sample.evidence,
            [f"inspection:{observation_id}"],
        )
        await conn.execute(
            """
            INSERT INTO inspection_observations (
                id, run_id, ordinal, check_id, target, status, reason, sampled_at,
                evidence, evidence_refs, initial
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                observation.id,
                run.id,
                check.ordinal,
                check.check_id,
                Jsonb(check.target),
                sample.status,
                sample.reason,
                sample.sampled_at,
                Jsonb(sample.evidence),
                Jsonb(observation.evidence_refs),
                initial,
            ),
        )
        return observation

    async def get_observation(self, observation_id: str) -> Observation | None:
        async with self._store.transaction() as conn:
            row = await _one(
                conn,
                "SELECT id, ordinal, check_id, target, status, reason, sampled_at, "
                "evidence, evidence_refs FROM inspection_observations WHERE id = %s",
                (observation_id,),
            )
            return Observation(**row) if row is not None else None

    async def publish_summary(self, run: InspectionRun) -> None:
        async with self._store.transaction() as conn:
            current, _ = await self.require_active(conn, run)
            await self._append_summary(conn, current)

    async def record_result(self, run: InspectionRun, result: dict) -> None:
        async with self._store.transaction() as conn:
            current, _ = await self.require_active(conn, run)
            await self._record_result(conn, current, result)

    async def record_receipt_result(
        self, conn: AsyncConnection, *, run_id: str, session_id: str, result: dict
    ) -> None:
        """调用方须在同一事务核对持久动作回执归属，不用于提交主线输出。"""
        await self._record_result(conn, await self._lock(conn, run_id, session_id), result)

    async def _record_result(self, conn: AsyncConnection, run: InspectionRun, result: dict) -> None:
        result = {**run.result, **project_payload(result)}
        await conn.execute(
            "UPDATE inspection_runs SET result = %s WHERE id = %s", (Jsonb(result), run.id)
        )
        await self._append_summary(conn, replace(run, result=result))

    async def _append_summary(self, conn: AsyncConnection, run: InspectionRun) -> None:
        rows = await conn.execute(
            """
            SELECT DISTINCT ON (ordinal) id, ordinal, check_id, target, status, reason,
                   sampled_at, evidence_refs
            FROM inspection_observations WHERE run_id = %s
            ORDER BY ordinal, recorded_order DESC
            """,
            (run.id,),
        )
        observations = [
            {
                **{
                    key: row[key]
                    for key in ("id", "check_id", "target", "status", "reason", "evidence_refs")
                },
                "sampled_at": row["sampled_at"].isoformat() if row["sampled_at"] else None,
            }
            for row in await rows.fetchall()
        ]
        await self.sessions.append_event(
            conn,
            run.session_id,
            "inspection_summary",
            project_payload(
                {
                    "run_id": run.id,
                    "status": run.status,
                    "reason": run.reason,
                    "cluster": run.cluster,
                    "scheduled_at": run.scheduled_at.isoformat(),
                    "deadline": run.deadline.isoformat(),
                    "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                    "sop_hash": run.sop_hash,
                    "observations": observations,
                    "result": run.result,
                }
            ),
            allow_closed=True,
        )

    async def enqueue_investigation(self, run: InspectionRun, text: str) -> Turn | None:
        async with self._store.transaction() as conn:
            current, _ = await self.require_active(conn, run)
            counts = await _one(
                conn,
                "SELECT count(*) AS total, bool_or(status = 'abnormal') AS abnormal "
                "FROM inspection_observations WHERE run_id = %s AND initial",
                (run.id,),
            )
            if counts["total"] != len(current.checks) or not counts["abnormal"]:
                return None
            return await self.sessions.enqueue_inspection_turn(conn, run.session_id, text)

    async def claim_investigation(self, run: InspectionRun, owner: str) -> Lease | None:
        async with self._store.transaction() as conn:
            await self.require_active(conn, run)
            if await self.sessions._active_turn(conn, run.session_id, "main") is None:
                return None
            return await self.sessions._acquire_lease(
                conn, run.session_id, owner, inspection_clock=self._clock
            )

    async def finish(
        self, run: InspectionRun, *, status: RunStatus = "completed", reason: str | None = None
    ) -> None:
        async with self._store.transaction() as conn:
            current = await self._lock(conn, run.id, run.session_id)
            if (
                current.status != "running"
                or current.owner != run.owner
                or current.generation != run.generation
            ):
                return
            now = await self.now(conn)
            if expired := self._expired(current, now):
                status, reason = expired
            await self._finish(conn, current, now, status, reason)

    async def _finish(
        self,
        conn: AsyncConnection,
        run: InspectionRun,
        now: datetime,
        status: RunStatus,
        reason: str | None,
    ) -> InspectionRun:
        if status == "running":
            raise ValueError("结轮需要终态")
        present = await conn.execute(
            "SELECT ordinal FROM inspection_observations WHERE run_id = %s AND initial", (run.id,)
        )
        recorded = {row["ordinal"] for row in await present.fetchall()}
        missing_sample = Sample.insufficient(
            "timeout" if status == "timed_out" else reason or "not_collected"
        )
        for check in run.checks:
            if check.ordinal not in recorded:
                await self._insert_observation(conn, run, check, missing_sample, True)

        await conn.execute(
            "UPDATE inspection_runs SET status = %s, reason = %s, finished_at = %s WHERE id = %s",
            (status, reason, now, run.id),
        )
        await conn.execute(
            """
            UPDATE investigations i
            SET status = 'aborted', finished_at = GREATEST(i.started_at, %s),
                termination_reason = %s
            FROM session_turns t
            WHERE t.id = i.session_turn_id AND t.session_id = %s AND i.status = 'running'
            """,
            (now, reason or "inspection_ended", run.session_id),
        )
        await conn.execute(
            "UPDATE session_turns SET state = 'aborted', finished_at = %s "
            "WHERE session_id = %s AND finished_at IS NULL",
            (now, run.session_id),
        )
        await conn.execute(
            "UPDATE sessions SET state = %s, closed_at = %s, "
            "lease_owner = NULL, lease_expires_at = NULL WHERE id = %s",
            (
                "interrupted" if status == "interrupted" else "closed",
                None if status == "interrupted" else now,
                run.session_id,
            ),
        )
        finished = replace(run, status=status, reason=reason, finished_at=now)
        await self._append_summary(conn, finished)
        return finished
