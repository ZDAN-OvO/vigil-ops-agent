"""PostgreSQL 业务表：迁移与调查记录读写。

checkpoint 表由 LangGraph 官方 saver 自己管理，不与这里的迁移混用。
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Self

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from aiops.contracts import (
    ActionAttempt,
    ActionProposal,
    ActionTarget,
    Approval,
    AttemptStatus,
    AuditEvent,
    Budget,
    BudgetUsage,
    Evidence,
    Hypothesis,
    Incident,
    Investigation,
    InvestigationStatus,
    ProposalStatus,
    RiskDecision,
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


class DuplicateProposal(RuntimeError):
    """同一目标版本已有进行中的提案。"""


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

    async def find_recent_incident(self, fingerprints: Sequence[str], *, since) -> Incident | None:
        """按告警指纹取最近的事件，用于基本去重。"""
        if not fingerprints:
            return None
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM incidents
                 WHERE alert_ids && %s AND created_at >= %s
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                (list(fingerprints), since),
            )
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

    async def list_incidents(self, limit: int = 20) -> list[Incident]:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM incidents ORDER BY created_at DESC, id LIMIT %s", (limit,)
            )
            rows = await cursor.fetchall()
        return [
            Incident(
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
            for row in rows
        ]

    async def list_investigations(self, incident_id: str) -> list[Investigation]:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM investigations WHERE incident_id = %s ORDER BY started_at, id",
                (incident_id,),
            )
            rows = await cursor.fetchall()
        return [
            Investigation(
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
            for row in rows
        ]

    async def mark_investigation_running(self, investigation_id: str, started_at) -> None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "UPDATE investigations SET status = 'running', started_at = %s WHERE id = %s",
                (started_at, investigation_id),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"调查不存在：{investigation_id}")

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

    async def save_proposal(self, proposal: ActionProposal, decision: RiskDecision) -> None:
        async with self._pool.connection() as conn:
            try:
                await conn.execute(
                    """
                    INSERT INTO action_proposals (
                        id, investigation_id, origin, kind, cluster, namespace, target_kind,
                        target_name, target_uid, target_resource_version, parameters,
                        evidence_ids, idempotency_key, reason, status, risk_disposition,
                        risk_reasons, policy_version, created_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        proposal.id,
                        proposal.investigation_id,
                        proposal.origin.value,
                        proposal.kind.value,
                        proposal.target.cluster,
                        proposal.target.namespace,
                        proposal.target.kind,
                        proposal.target.name,
                        proposal.target.uid,
                        proposal.target.resource_version,
                        Jsonb(proposal.parameters),
                        proposal.evidence_ids,
                        proposal.idempotency_key,
                        proposal.reason,
                        proposal.status.value,
                        decision.disposition.value,
                        decision.reasons,
                        decision.policy_version,
                        proposal.created_at,
                    ),
                )
            except psycopg.errors.UniqueViolation as exc:
                raise DuplicateProposal("同一目标版本已有进行中的提案") from exc

    async def get_proposal(self, proposal_id: str) -> ActionProposal | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM action_proposals WHERE id = %s", (proposal_id,)
            )
            row = await cursor.fetchone()
        return _proposal_from_row(row) if row else None

    async def list_proposals(
        self, *, status: ProposalStatus | None = None, limit: int = 50
    ) -> list[ActionProposal]:
        async with self._pool.connection() as conn:
            if status is None:
                cursor = await conn.execute(
                    "SELECT * FROM action_proposals ORDER BY created_at DESC, id LIMIT %s",
                    (limit,),
                )
            else:
                cursor = await conn.execute(
                    """
                    SELECT * FROM action_proposals
                     WHERE status = %s ORDER BY created_at DESC, id LIMIT %s
                    """,
                    (status.value, limit),
                )
            rows = await cursor.fetchall()
        return [_proposal_from_row(row) for row in rows]

    async def set_proposal_status(self, proposal_id: str, status: ProposalStatus) -> None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "UPDATE action_proposals SET status = %s WHERE id = %s",
                (status.value, proposal_id),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"提案不存在：{proposal_id}")

    async def save_approval(self, approval: Approval) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO approvals (
                    id, proposal_id, proposal_hash, actor, decision, reason,
                    decided_at, expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    approval.id,
                    approval.proposal_id,
                    approval.proposal_hash,
                    approval.actor,
                    approval.decision.value,
                    approval.reason,
                    approval.decided_at,
                    approval.expires_at,
                ),
            )

    async def get_approval(self, proposal_id: str) -> Approval | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM approvals WHERE proposal_id = %s ORDER BY decided_at DESC LIMIT 1",
                (proposal_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return Approval(
            id=row["id"],
            proposal_id=row["proposal_id"],
            proposal_hash=row["proposal_hash"],
            actor=row["actor"],
            decision=row["decision"],
            reason=row["reason"],
            decided_at=row["decided_at"],
            expires_at=row["expires_at"],
        )

    async def save_attempt(self, attempt: ActionAttempt) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO action_attempts (
                    id, proposal_id, status, idempotency_key, snapshot, request,
                    result, verification, started_at, finished_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    attempt.id,
                    attempt.proposal_id,
                    attempt.status.value,
                    attempt.idempotency_key,
                    Jsonb(attempt.snapshot),
                    Jsonb(attempt.request),
                    Jsonb(attempt.result) if attempt.result is not None else None,
                    Jsonb(attempt.verification) if attempt.verification is not None else None,
                    attempt.started_at,
                    attempt.finished_at,
                ),
            )

    async def update_attempt(
        self,
        attempt_id: str,
        *,
        status: AttemptStatus,
        result: dict | None = None,
        verification: dict | None = None,
        finished_at=None,
    ) -> None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE action_attempts
                   SET status = %s,
                       result = COALESCE(%s, result),
                       verification = COALESCE(%s, verification),
                       finished_at = COALESCE(%s, finished_at)
                 WHERE id = %s
                """,
                (
                    status.value,
                    Jsonb(result) if result is not None else None,
                    Jsonb(verification) if verification is not None else None,
                    finished_at,
                    attempt_id,
                ),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"执行记录不存在：{attempt_id}")

    async def get_attempt(self, proposal_id: str) -> ActionAttempt | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM action_attempts WHERE proposal_id = %s", (proposal_id,)
            )
            row = await cursor.fetchone()
        return _attempt_from_row(row) if row else None

    async def acquire_lock(self, key: str, proposal_id: str) -> bool:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO action_locks (target_key, proposal_id) VALUES (%s, %s)
                ON CONFLICT (target_key) DO NOTHING
                """,
                (key, proposal_id),
            )
            return cursor.rowcount == 1

    async def release_lock(self, key: str, proposal_id: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM action_locks WHERE target_key = %s AND proposal_id = %s",
                (key, proposal_id),
            )

    async def target_is_locked(self, key: str) -> bool:
        async with self._pool.connection() as conn:
            cursor = await conn.execute("SELECT 1 FROM action_locks WHERE target_key = %s", (key,))
            return await cursor.fetchone() is not None


def _proposal_from_row(row: dict) -> ActionProposal:
    return ActionProposal(
        id=row["id"],
        investigation_id=row["investigation_id"],
        origin=row["origin"],
        kind=row["kind"],
        target=ActionTarget(
            cluster=row["cluster"],
            namespace=row["namespace"],
            kind=row["target_kind"],
            name=row["target_name"],
            uid=row["target_uid"],
            resource_version=row["target_resource_version"],
        ),
        parameters=row["parameters"],
        evidence_ids=row["evidence_ids"],
        idempotency_key=row["idempotency_key"],
        reason=row["reason"],
        status=row["status"],
        created_at=row["created_at"],
    )


def _attempt_from_row(row: dict) -> ActionAttempt:
    return ActionAttempt(
        id=row["id"],
        proposal_id=row["proposal_id"],
        status=row["status"],
        idempotency_key=row["idempotency_key"],
        snapshot=row["snapshot"],
        request=row["request"],
        result=row["result"],
        verification=row["verification"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )
