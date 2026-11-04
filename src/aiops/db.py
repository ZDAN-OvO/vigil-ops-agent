"""PostgreSQL 业务表：迁移与调查记录读写。

checkpoint 表由 LangGraph 官方 saver 自己管理，不与这里的迁移混用。
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Self

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from aiops.contracts import (
    AuditEvent,
    Budget,
    BudgetUsage,
    Evidence,
    Hypothesis,
    Incident,
    Investigation,
    InvestigationStatus,
    TimeRange,
    ToolCall,
)

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
SCHEMA_MIGRATIONS = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    " version text PRIMARY KEY,"
    " applied_at timestamptz NOT NULL DEFAULT now())"
)


class StoreUnavailable(RuntimeError):
    """数据库不可用时抛出，调用方不得把它当成调查结论。"""


def migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


class Store:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    @classmethod
    @asynccontextmanager
    async def open(cls, dsn: str) -> AsyncIterator[Self]:
        pool = AsyncConnectionPool(
            dsn,
            min_size=1,
            max_size=4,
            open=False,
            kwargs={"row_factory": dict_row},
        )
        try:
            await pool.open(wait=True, timeout=10)
        except Exception as exc:  # noqa: BLE001 - 统一转换为可识别的启动失败
            await pool.close()
            raise StoreUnavailable(f"无法连接 PostgreSQL：{exc}") from exc
        try:
            yield cls(pool)
        finally:
            await pool.close()

    async def migrate(self) -> list[str]:
        """按文件名顺序应用未执行的迁移，返回本次应用的版本。"""
        applied: list[str] = []
        async with self._pool.connection() as conn:
            await conn.execute(SCHEMA_MIGRATIONS)
            cursor = await conn.execute("SELECT version FROM schema_migrations")
            done = {row["version"] for row in await cursor.fetchall()}
            for path in migration_files():
                if path.stem in done:
                    continue
                async with conn.transaction():
                    await conn.execute(path.read_text(encoding="utf-8"))
                    await conn.execute(
                        "INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,)
                    )
                applied.append(path.stem)
        return applied

    async def applied_migrations(self) -> list[str]:
        async with self._pool.connection() as conn:
            cursor = await conn.execute("SELECT version FROM schema_migrations ORDER BY version")
            return [row["version"] for row in await cursor.fetchall()]

    async def save_incident(self, incident: Incident) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO incidents (
                    id, environment, cluster, namespace, service, trigger, symptoms,
                    time_start, time_end, alert_ids, context
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    incident.id,
                    incident.environment,
                    incident.cluster,
                    incident.namespace,
                    incident.service,
                    incident.trigger,
                    incident.symptoms,
                    incident.time_range.start,
                    incident.time_range.end,
                    incident.alert_ids,
                    incident.context,
                ),
            )

    async def get_incident(self, incident_id: str) -> Incident | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute("SELECT * FROM incidents WHERE id = %s", (incident_id,))
            row = await cursor.fetchone()
        if row is None:
            return None
        return Incident(
            id=row["id"],
            environment=row["environment"],
            cluster=row["cluster"],
            namespace=row["namespace"],
            service=row["service"],
            trigger=row["trigger"],
            symptoms=row["symptoms"],
            time_range=TimeRange(start=row["time_start"], end=row["time_end"]),
            alert_ids=row["alert_ids"],
            context=row["context"],
        )

    async def create_investigation(self, investigation: Investigation) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO investigations (
                    id, incident_id, parent_run_id, status, model, policy_version,
                    budget_tool_calls, budget_model_calls, budget_wall_clock_seconds,
                    used_tool_calls, used_model_calls, conclusion, termination_reason,
                    started_at, finished_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    investigation.id,
                    investigation.incident_id,
                    investigation.parent_run_id,
                    investigation.status.value,
                    investigation.model,
                    investigation.policy_version,
                    investigation.budget.max_tool_calls,
                    investigation.budget.max_model_calls,
                    investigation.budget.max_wall_clock_seconds,
                    investigation.usage.tool_calls,
                    investigation.usage.model_calls,
                    investigation.conclusion,
                    investigation.termination_reason,
                    investigation.started_at,
                    investigation.finished_at,
                ),
            )

    async def get_investigation(self, investigation_id: str) -> Investigation | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM investigations WHERE id = %s", (investigation_id,)
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return Investigation(
            id=row["id"],
            incident_id=row["incident_id"],
            parent_run_id=row["parent_run_id"],
            status=InvestigationStatus(row["status"]),
            model=row["model"],
            policy_version=row["policy_version"],
            budget=Budget(
                max_tool_calls=row["budget_tool_calls"],
                max_model_calls=row["budget_model_calls"],
                max_wall_clock_seconds=row["budget_wall_clock_seconds"],
            ),
            usage=BudgetUsage(
                tool_calls=row["used_tool_calls"],
                model_calls=row["used_model_calls"],
            ),
            conclusion=row["conclusion"],
            termination_reason=row["termination_reason"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    async def set_investigation_status(
        self,
        investigation_id: str,
        status: InvestigationStatus,
        *,
        conclusion: str | None = None,
        termination_reason: str | None = None,
        finished_at=None,
    ) -> None:
        """写入终止语义；不合法组合直接拒绝，避免把中断伪装成结论。"""
        if status is InvestigationStatus.COMPLETED and not conclusion:
            raise ValueError("completed 必须给出根因候选")
        if status.terminal and finished_at is None:
            raise ValueError("终止状态必须记录结束时间")
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE investigations
                   SET status = %s, conclusion = %s, termination_reason = %s, finished_at = %s
                 WHERE id = %s
                """,
                (status.value, conclusion, termination_reason, finished_at, investigation_id),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"调查不存在：{investigation_id}")

    async def add_usage(self, investigation_id: str, *, tool_calls: int = 0, model_calls: int = 0):
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE investigations
                   SET used_tool_calls = used_tool_calls + %s,
                       used_model_calls = used_model_calls + %s
                 WHERE id = %s
                """,
                (tool_calls, model_calls, investigation_id),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"调查不存在：{investigation_id}")

    async def save_hypothesis(self, hypothesis: Hypothesis) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO hypotheses (
                    id, investigation_id, parent_id, statement, expected_observations,
                    origin, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    hypothesis.id,
                    hypothesis.investigation_id,
                    hypothesis.parent_id,
                    hypothesis.statement,
                    hypothesis.expected_observations,
                    hypothesis.origin,
                    hypothesis.status.value,
                ),
            )

    async def list_hypotheses(self, investigation_id: str) -> list[Hypothesis]:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM hypotheses WHERE investigation_id = %s ORDER BY created_at, id",
                (investigation_id,),
            )
            rows = await cursor.fetchall()
        return [
            Hypothesis(
                id=row["id"],
                investigation_id=row["investigation_id"],
                parent_id=row["parent_id"],
                statement=row["statement"],
                expected_observations=row["expected_observations"],
                origin=row["origin"],
                status=row["status"],
            )
            for row in rows
        ]

    async def set_hypothesis_status(self, hypothesis_id: str, status: str) -> None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "UPDATE hypotheses SET status = %s WHERE id = %s", (status, hypothesis_id)
            )
            if cursor.rowcount != 1:
                raise LookupError(f"假设不存在：{hypothesis_id}")

    async def set_evidence_relation(self, evidence_id: str, relation: str) -> None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "UPDATE evidence SET relation = %s WHERE id = %s", (relation, evidence_id)
            )
            if cursor.rowcount != 1:
                raise LookupError(f"证据不存在：{evidence_id}")

    async def save_tool_call(self, call: ToolCall) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO tool_calls (
                    id, investigation_id, tool, parameters, time_start, time_end,
                    started_at, finished_at, error_code, retry_count, truncated, raw_ref
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    call.id,
                    call.investigation_id,
                    call.tool,
                    Jsonb(call.parameters),
                    call.time_range.start,
                    call.time_range.end,
                    call.started_at,
                    call.finished_at,
                    call.error_code,
                    call.retry_count,
                    call.truncated,
                    call.raw_ref,
                ),
            )

    async def list_tool_calls(self, investigation_id: str) -> list[ToolCall]:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM tool_calls WHERE investigation_id = %s ORDER BY started_at, id",
                (investigation_id,),
            )
            rows = await cursor.fetchall()
        return [
            ToolCall(
                id=row["id"],
                investigation_id=row["investigation_id"],
                tool=row["tool"],
                parameters=row["parameters"],
                time_range=TimeRange(start=row["time_start"], end=row["time_end"]),
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                error_code=row["error_code"],
                retry_count=row["retry_count"],
                truncated=row["truncated"],
                raw_ref=row["raw_ref"],
            )
            for row in rows
        ]

    async def save_evidence(self, evidence: Evidence) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO evidence (
                    id, investigation_id, hypothesis_id, tool_call_id, source,
                    independence_key, observation, relation, raw_ref, time_start, time_end
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    evidence.id,
                    evidence.investigation_id,
                    evidence.hypothesis_id,
                    evidence.tool_call_id,
                    evidence.source,
                    evidence.independence_key,
                    evidence.observation,
                    evidence.relation,
                    evidence.raw_ref,
                    evidence.time_range.start,
                    evidence.time_range.end,
                ),
            )

    async def list_evidence(self, investigation_id: str) -> list[Evidence]:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM evidence WHERE investigation_id = %s ORDER BY created_at, id",
                (investigation_id,),
            )
            rows = await cursor.fetchall()
        return [
            Evidence(
                id=row["id"],
                investigation_id=row["investigation_id"],
                hypothesis_id=row["hypothesis_id"],
                tool_call_id=row["tool_call_id"],
                source=row["source"],
                independence_key=row["independence_key"],
                observation=row["observation"],
                relation=row["relation"],
                raw_ref=row["raw_ref"],
                time_range=TimeRange(start=row["time_start"], end=row["time_end"]),
            )
            for row in rows
        ]

    async def append_audit(self, events: Sequence[AuditEvent]) -> None:
        async with self._pool.connection() as conn:
            for event in events:
                await conn.execute(
                    """
                    INSERT INTO audit_events (
                        id, correlation_id, actor, event, occurred_at, payload
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        event.id,
                        event.correlation_id,
                        event.actor,
                        event.event,
                        event.occurred_at,
                        Jsonb(event.payload),
                    ),
                )

    async def list_audit(self, correlation_id: str) -> list[AuditEvent]:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM audit_events WHERE correlation_id = %s ORDER BY occurred_at, id",
                (correlation_id,),
            )
            rows = await cursor.fetchall()
        return [AuditEvent(**row) for row in rows]
