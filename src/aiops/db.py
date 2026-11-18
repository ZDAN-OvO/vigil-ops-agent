"""PostgreSQL 业务表：迁移与调查记录读写。

checkpoint 表由 LangGraph 官方 saver 自己管理，不与这里的迁移混用。
"""

import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from aiops.contracts import (
    ActionAttempt,
    ActionFollowup,
    ActionProposal,
    ActionTarget,
    Approval,
    AttemptOutcome,
    AttemptStatus,
    AuditEvent,
    Budget,
    BudgetUsage,
    Delegation,
    DelegationStatus,
    FollowupStatus,
    Hypothesis,
    HypothesisEvidence,
    Incident,
    Investigation,
    InvestigationStatus,
    InvestigationTask,
    Observation,
    ProposalStatus,
    RiskDecision,
    SkillUsage,
    TaskPhase,
    TimeRange,
    ToolRequest,
    ToolRequestState,
)
from aiops.knowledge import Runbook

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


class DuplicateAttempt(RuntimeError):
    """同一提案已有执行记录，不得再次写入。"""


class ConversationConflict(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class WebRequestResult:
    id: str
    replayed: bool = False


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
            return await self._insert_incident(conn, incident)

    async def _insert_incident(self, conn: psycopg.AsyncConnection, incident: Incident) -> None:
        await conn.execute(
            """
            INSERT INTO incidents (
                id, environment, cluster, namespace, service, trigger, symptoms,
                time_start, time_end, alert_ids, alert_names, context
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                incident.alert_names,
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
            alert_names=row.get("alert_names") or [],
            context=row["context"],
        )

    async def create_investigation(self, investigation: Investigation) -> None:
        async with self._pool.connection() as conn:
            return await self._insert_investigation(conn, investigation)

    async def _insert_investigation(
        self, conn: psycopg.AsyncConnection, investigation: Investigation
    ) -> None:
        await conn.execute(
            """
            INSERT INTO investigations (
                id, incident_id, parent_run_id, status, model, policy_version,
                budget_tool_calls, budget_model_calls, budget_wall_clock_seconds,
                budget_action_seconds, used_tool_calls, used_model_calls, conclusion,
                termination_reason, started_at, finished_at, skills
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                investigation.budget.max_action_seconds,
                investigation.usage.tool_calls,
                investigation.usage.model_calls,
                investigation.conclusion,
                investigation.termination_reason,
                investigation.started_at,
                investigation.finished_at,
                Jsonb([item.model_dump(mode="json") for item in investigation.skills]),
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

    async def get_incident_created_at(self, incident_id: str) -> datetime | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT created_at FROM incidents WHERE id = %s", (incident_id,)
            )
            row = await cursor.fetchone()
        return row["created_at"] if row else None

    async def list_incident_receipts(self, fingerprint: str) -> list[datetime]:
        """同一告警指纹的全部事件接收时间，与 webhook 去重使用 created_at。"""
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT created_at FROM incidents
                 WHERE %s = ANY (alert_ids)
                 ORDER BY created_at
                """,
                (fingerprint,),
            )
            rows = await cursor.fetchall()
        return [row["created_at"] for row in rows]

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
                    max_action_seconds=row["budget_action_seconds"],
                ),
                usage=BudgetUsage(
                    tool_calls=row["used_tool_calls"],
                    model_calls=row["used_model_calls"],
                ),
                skills=[SkillUsage.model_validate(item) for item in (row.get("skills") or [])],
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
                max_action_seconds=row["budget_action_seconds"],
            ),
            usage=BudgetUsage(
                tool_calls=row["used_tool_calls"],
                model_calls=row["used_model_calls"],
            ),
            skills=[SkillUsage.model_validate(item) for item in (row.get("skills") or [])],
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
                    id, investigation_id, parent_id, statement,
                    origin, status, update_reason
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    hypothesis.id,
                    hypothesis.investigation_id,
                    hypothesis.parent_id,
                    hypothesis.statement,
                    hypothesis.origin,
                    hypothesis.status.value,
                    hypothesis.update_reason,
                ),
            )

    async def get_hypothesis(self, hypothesis_id: str) -> Hypothesis | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute("SELECT * FROM hypotheses WHERE id = %s", (hypothesis_id,))
            row = await cursor.fetchone()
        return _hypothesis_from_row(row) if row else None

    async def update_hypothesis(
        self,
        hypothesis_id: str,
        *,
        statement: str,
        status: str,
        update_reason: str,
        parent_id: str | None = None,
        update_parent: bool = False,
    ) -> Hypothesis:
        """改假设内容或状态；不保留修订历史，变更理由写在 update_reason。"""
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE hypotheses
                   SET statement = %s,
                       status = %s,
                       update_reason = %s,
                       parent_id = CASE WHEN %s THEN %s ELSE parent_id END
                 WHERE id = %s
             RETURNING *
                """,
                (
                    statement,
                    status,
                    update_reason,
                    update_parent,
                    parent_id,
                    hypothesis_id,
                ),
            )
            row = await cursor.fetchone()
            if row is None:
                raise LookupError(f"假设不存在：{hypothesis_id}")
        return _hypothesis_from_row(row)

    async def list_hypotheses(self, investigation_id: str) -> list[Hypothesis]:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM hypotheses WHERE investigation_id = %s ORDER BY created_at, id",
                (investigation_id,),
            )
            rows = await cursor.fetchall()
        return [_hypothesis_from_row(row) for row in rows]

    async def set_hypothesis_status(self, hypothesis_id: str, status: str) -> None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "UPDATE hypotheses SET status = %s WHERE id = %s", (status, hypothesis_id)
            )
            if cursor.rowcount != 1:
                raise LookupError(f"假设不存在：{hypothesis_id}")

    async def upsert_runbook(self, runbook: Runbook) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO runbooks (
                    id, version, title, summary, applies_to, symptoms, steps, source, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (id) DO UPDATE SET
                    version = EXCLUDED.version,
                    title = EXCLUDED.title,
                    summary = EXCLUDED.summary,
                    applies_to = EXCLUDED.applies_to,
                    symptoms = EXCLUDED.symptoms,
                    steps = EXCLUDED.steps,
                    source = EXCLUDED.source,
                    updated_at = now()
                """,
                (
                    runbook.id,
                    runbook.version,
                    runbook.title,
                    runbook.summary,
                    list(runbook.applies_to),
                    list(runbook.symptoms),
                    Jsonb(list(runbook.steps)),
                    runbook.source,
                ),
            )

    async def list_runbooks(
        self, *, service: str | None = None, query: str | None = None, limit: int = 3
    ) -> list[Runbook]:
        clauses = ["(applies_to = '{}' OR %s = ANY(applies_to))"] if service else []
        params: list[object] = [service] if service else []
        if query:
            clauses.append(
                "(title ILIKE %s OR summary ILIKE %s OR array_to_string(symptoms, ' ') ILIKE %s)"
            )
            like = f"%{query}%"
            params.extend([like, like, like])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"SELECT * FROM runbooks {where} ORDER BY id LIMIT %s", tuple(params)
            )
            rows = await cursor.fetchall()
        return [self._runbook(row) for row in rows]

    async def get_runbook(self, runbook_id: str) -> Runbook | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute("SELECT * FROM runbooks WHERE id = %s", (runbook_id,))
            row = await cursor.fetchone()
        return self._runbook(row) if row else None

    @staticmethod
    def _runbook(row) -> Runbook:
        return Runbook(
            id=row["id"],
            version=row["version"],
            title=row["title"],
            summary=row["summary"],
            applies_to=tuple(row["applies_to"] or ()),
            symptoms=tuple(row["symptoms"] or ()),
            steps=tuple(row["steps"] or ()),
            source=row["source"],
        )

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

    async def count_proposals_by_disposition(self, disposition: str) -> int:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT count(*) FROM action_proposals WHERE risk_disposition = %s", (disposition,)
            )
            row = await cursor.fetchone()
        return int(next(iter(row.values())))

    async def save_proposal(self, proposal: ActionProposal, decision: RiskDecision) -> None:
        async with self._pool.connection() as conn:
            await self._insert_proposal(conn, proposal, decision)

    async def save_proposal_with_approval(
        self, proposal: ActionProposal, decision: RiskDecision, approval: Approval
    ) -> None:
        """自动允许时提案与策略授权同一事务写入，崩溃后不留下没有授权的 approved。"""
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await self._insert_proposal(conn, proposal, decision)
                await self._insert_approval(conn, approval)

    async def _insert_proposal(
        self, conn: psycopg.AsyncConnection, proposal: ActionProposal, decision: RiskDecision
    ) -> None:
        try:
            await conn.execute(
                """
                INSERT INTO action_proposals (
                    id, investigation_id, origin, kind, cluster, namespace, target_kind,
                    target_name, target_uid, target_resource_version, parameters,
                    evidence_ids, idempotency_key, reason, status, risk_disposition,
                    risk_reasons, policy_version, created_at, purpose, hypothesis_id,
                    prediction, observation_conditions, observation_window_seconds
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s
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
                    proposal.purpose.value,
                    proposal.hypothesis_id,
                    proposal.prediction,
                    Jsonb(proposal.observation_conditions),
                    proposal.observation_window_seconds,
                ),
            )
        except psycopg.errors.UniqueViolation as exc:
            raise DuplicateProposal("同一目标版本已有进行中的提案") from exc

    async def latest_proposal_blocks_auto(
        self,
        cluster: str,
        namespace: str,
        workload: str,
        replica_set: str = "",
        *,
        exclude_proposal: str = "",
    ) -> bool:
        """同一集群、同一工作负载最近一次动作为未成功结束时，后续不再自动执行。

        只看已经执行过或仍持有授权的提案：待审批、策略拒绝、人工拒绝和过期都不
        解除停止状态。已授权未执行、执行中、失败和未知都阻断，只有成功或没有记录
        才放行。工作负载用 Deployment UID 关联；历史提案没有该字段时退回
        ReplicaSet 名称或目标 UID（旧的 rollout_undo 只记 target_uid）。
        """
        if not workload and not replica_set:
            return False
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT status
                  FROM action_proposals
                 WHERE cluster = %s
                   AND namespace = %s
                   AND status NOT IN ('pending_approval', 'denied', 'rejected', 'expired')
                   AND (
                        parameters->>'workload' = %s
                        OR (
                            NOT (parameters ? 'workload')
                            AND (
                                parameters->>'replica_set' = %s
                                OR target_uid = %s
                            )
                        )
                   )
                   AND id != %s
                 ORDER BY created_at DESC, id DESC
                 LIMIT 1
                """,
                (cluster, namespace, workload, replica_set, workload, exclude_proposal),
            )
            row = await cursor.fetchone()
        return bool(row and row["status"] in ("approved", "executing", "failed", "unknown"))

    async def get_proposal_disposition(self, proposal_id: str) -> str | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT risk_disposition FROM action_proposals WHERE id = %s", (proposal_id,)
            )
            row = await cursor.fetchone()
        return str(row["risk_disposition"]) if row else None

    async def get_proposal(self, proposal_id: str) -> ActionProposal | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM action_proposals WHERE id = %s", (proposal_id,)
            )
            row = await cursor.fetchone()
        return _proposal_from_row(row) if row else None

    async def list_proposals_for_investigation(self, investigation_id: str) -> list[ActionProposal]:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM action_proposals
                 WHERE investigation_id = %s
                 ORDER BY created_at, id
                """,
                (investigation_id,),
            )
            rows = await cursor.fetchall()
        return [_proposal_from_row(row) for row in rows]

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

    async def transition_proposal(
        self, proposal_id: str, *, expected: ProposalStatus, target: ProposalStatus
    ) -> bool:
        """仅当当前状态为 expected 时迁移；返回是否抢到这次迁移。"""
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "UPDATE action_proposals SET status = %s WHERE id = %s AND status = %s",
                (target.value, proposal_id, expected.value),
            )
            return cursor.rowcount == 1

    async def save_approval(self, approval: Approval) -> None:
        async with self._pool.connection() as conn:
            await self._insert_approval(conn, approval)

    async def _insert_approval(self, conn: psycopg.AsyncConnection, approval: Approval) -> None:
        await conn.execute(
            """
            INSERT INTO approvals (
                id, proposal_id, proposal_hash, actor, decision, reason,
                decided_at, expires_at, card_hash
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                approval.card_hash,
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
            card_hash=row["card_hash"],
        )

    async def save_attempt(self, attempt: ActionAttempt) -> None:
        async with self._pool.connection() as conn:
            await self._insert_attempt(conn, attempt)

    async def _insert_attempt(self, conn: psycopg.AsyncConnection, attempt: ActionAttempt) -> None:
        try:
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
        except psycopg.errors.UniqueViolation as exc:
            raise DuplicateAttempt("该提案已有执行记录") from exc

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
                       result = CASE WHEN %s::jsonb IS NULL THEN result
                                     ELSE COALESCE(result, '{}'::jsonb) || %s::jsonb END,
                       verification = COALESCE(%s, verification),
                       finished_at = COALESCE(%s, finished_at)
                 WHERE id = %s
                """,
                (
                    status.value,
                    Jsonb(result) if result is not None else None,
                    Jsonb(result) if result is not None else None,
                    Jsonb(verification) if verification is not None else None,
                    finished_at,
                    attempt_id,
                ),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"执行记录不存在：{attempt_id}")

    async def get_conversation_for_investigation(self, investigation_id: str) -> dict | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM conversations WHERE investigation_id = %s", (investigation_id,)
            )
            return await cur.fetchone()

    async def register_rollback_sending(
        self, attempt: ActionAttempt, *, investigation_id: str | None, instance_id: str | None
    ) -> bool:
        """持有任务行锁时核对执行归属，然后在同一事务中登记发写请求。"""
        async with self._pool.connection() as conn:
            if instance_id is not None:
                cur = await conn.execute(
                    """
                    SELECT investigation_id FROM investigation_tasks
                     WHERE investigation_id = %s AND phase = 'running'
                       AND lease_owner = %s AND lease_expires_at > clock_timestamp()
                     FOR UPDATE
                    """,
                    (investigation_id, instance_id),
                )
                if await cur.fetchone() is None:
                    return False
                cur = await conn.execute(
                    """
                    SELECT 1 FROM web_work w JOIN conversations c ON c.id = w.conversation_id
                     WHERE c.investigation_id = %s AND c.finished_at IS NULL
                       AND w.state = 'running' AND w.instance_id = %s AND w.proposal_id = %s
                    """,
                    (investigation_id, instance_id, attempt.proposal_id),
                )
                if await cur.fetchone() is None:
                    return False
            cur = await conn.execute(
                """
                UPDATE action_proposals SET status = 'executing'
                 WHERE id = %s AND status = 'approved'
                """,
                (attempt.proposal_id,),
            )
            if not cur.rowcount:
                return False
            await self._insert_attempt(conn, attempt)
            return True

    async def record_not_sent(
        self, attempt: ActionAttempt, *, proposal_status: ProposalStatus, target_lock_key: str
    ) -> None:
        """写前失败的记录和目标解锁一起提交。"""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE action_proposals SET status = %s WHERE id = %s AND status = 'approved'",
                (proposal_status.value, attempt.proposal_id),
            )
            if cur.rowcount:
                await self._insert_attempt(conn, attempt)
            await conn.execute(
                "DELETE FROM action_locks WHERE target_key = %s AND proposal_id = %s",
                (target_lock_key, attempt.proposal_id),
            )

    async def finish_attempt(
        self,
        attempt_id: str,
        *,
        proposal_id: str,
        proposal_status: ProposalStatus,
        status: AttemptStatus,
        result: dict | None,
        verification: dict | None,
        finished_at,
        followup: ActionFollowup | None = None,
        target_lock_key: str | None = None,
    ) -> None:
        """执行记录、提案状态和待办同一事务落库，避免只写一半。"""
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cursor = await conn.execute(
                    """
                    UPDATE action_attempts
                       SET status = %s,
                           result = CASE WHEN %s::jsonb IS NULL THEN result
                                         ELSE COALESCE(result, '{}'::jsonb) || %s::jsonb END,
                           verification = COALESCE(%s, verification),
                           finished_at = COALESCE(%s, finished_at)
                     WHERE id = %s
                    """,
                    (
                        status.value,
                        Jsonb(result) if result is not None else None,
                        Jsonb(result) if result is not None else None,
                        Jsonb(verification) if verification is not None else None,
                        finished_at,
                        attempt_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise LookupError(f"执行记录不存在：{attempt_id}")
                await conn.execute(
                    "UPDATE action_proposals SET status = %s WHERE id = %s",
                    (proposal_status.value, proposal_id),
                )
                if followup is not None:
                    await self._insert_followup(conn, followup)
                elif target_lock_key is not None:
                    await conn.execute(
                        "DELETE FROM action_locks WHERE target_key = %s AND proposal_id = %s",
                        (target_lock_key, proposal_id),
                    )

    async def _insert_followup(
        self, conn: psycopg.AsyncConnection, followup: ActionFollowup
    ) -> None:
        await conn.execute(
            """
            INSERT INTO action_followups (
                id, proposal_id, attempt_id, cluster, namespace, target_kind,
                target_name, outcome, reason, evidence, status, created_at,
                resolved_at, resolved_by, resolution
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                followup.id,
                followup.proposal_id,
                followup.attempt_id,
                followup.cluster,
                followup.namespace,
                followup.target_kind,
                followup.target_name,
                followup.outcome.value,
                followup.reason,
                Jsonb(followup.evidence),
                followup.status.value,
                followup.created_at,
                followup.resolved_at,
                followup.resolved_by,
                followup.resolution,
            ),
        )

    async def list_followups(
        self,
        *,
        status: FollowupStatus | None = None,
        proposal_id: str | None = None,
        limit: int = 50,
    ) -> list[ActionFollowup]:
        clauses = []
        values: list[object] = []
        if status is not None:
            clauses.append("status = %s")
            values.append(status.value)
        if proposal_id is not None:
            clauses.append("proposal_id = %s")
            values.append(proposal_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(limit)
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"""
                SELECT * FROM action_followups {where}
                 ORDER BY created_at DESC, id DESC LIMIT %s
                """,
                tuple(values),
            )
            rows = await cursor.fetchall()
        return [_followup_from_row(row) for row in rows]

    async def get_followup_by_attempt(self, attempt_id: str) -> ActionFollowup | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM action_followups WHERE attempt_id = %s", (attempt_id,)
            )
            row = await cursor.fetchone()
        return _followup_from_row(row) if row else None

    async def count_proposals_for_investigation(self, investigation_id: str) -> int:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT count(*) AS total FROM action_proposals WHERE investigation_id = %s",
                (investigation_id,),
            )
            row = await cursor.fetchone()
        return int(row["total"]) if row else 0

    async def count_approvals(self, proposal_id: str) -> int:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT count(*) AS total FROM approvals WHERE proposal_id = %s",
                (proposal_id,),
            )
            row = await cursor.fetchone()
        return int(row["total"]) if row else 0

    async def get_followup(self, followup_id: str) -> ActionFollowup | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM action_followups WHERE id = %s", (followup_id,)
            )
            row = await cursor.fetchone()
        return _followup_from_row(row) if row else None

    async def resolve_followup(
        self, followup_id: str, *, actor: str, note: str
    ) -> ActionFollowup | None:
        if not actor.strip() or not note.strip():
            raise ValueError("解决待办必须记录操作者与说明")
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    """
                    SELECT t.investigation_id
                      FROM action_followups f JOIN action_proposals p ON p.id = f.proposal_id
                      JOIN investigation_tasks t USING (investigation_id)
                     WHERE f.id = %s FOR UPDATE OF t
                    """,
                    (followup_id,),
                )
                await cur.fetchone()
                cursor = await conn.execute(
                    """
                    UPDATE action_followups
                       SET status = %s, resolved_at = %s, resolved_by = %s, resolution = %s
                     WHERE id = %s AND status = %s
                    """,
                    (
                        FollowupStatus.RESOLVED.value,
                        datetime.now(UTC),
                        actor,
                        note,
                        followup_id,
                        FollowupStatus.OPEN.value,
                    ),
                )
                if cursor.rowcount != 1:
                    return None
                cursor = await conn.execute(
                    "SELECT * FROM action_followups WHERE id = %s", (followup_id,)
                )
                row = await cursor.fetchone()
                if row is None:
                    return None
                await conn.execute(
                    """
                    INSERT INTO audit_events (
                        id, correlation_id, actor, event, occurred_at, payload
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        f"audit-{uuid.uuid4().hex[:12]}",
                        row["proposal_id"],
                        actor,
                        "action.followup_resolved",
                        datetime.now(UTC),
                        Jsonb({"followup_id": followup_id, "note": note}),
                    ),
                )
                await conn.execute(
                    "UPDATE action_proposals SET status = 'failed' "
                    "WHERE id = %s AND status = 'unknown'",
                    (row["proposal_id"],),
                )
                await conn.execute(
                    "DELETE FROM action_locks WHERE proposal_id = %s", (row["proposal_id"],)
                )
                await conn.execute(
                    """
                    INSERT INTO conversation_events (conversation_id, kind)
                    SELECT c.id, 'followup' FROM conversations c
                    JOIN action_proposals p USING (investigation_id)
                    WHERE p.id = %s
                    """,
                    (row["proposal_id"],),
                )
        return _followup_from_row(row)

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

    # ---- 任务运行记录：阶段、预算用量、暂停与租约 ----

    async def create_task(self, task: InvestigationTask) -> bool:
        """新建任务记录；已存在时返回 False，不覆写历史用量。"""
        async with self._pool.connection() as conn:
            return await self._insert_task(conn, task)

    async def _insert_task(self, conn: psycopg.AsyncConnection, task: InvestigationTask) -> bool:
        cursor = await conn.execute(
            """
            INSERT INTO investigation_tasks (
                investigation_id, phase, runtime_version, calendar_started_at,
                used_model_calls, used_tool_calls, investigation_seconds, action_seconds
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (investigation_id) DO NOTHING
            """,
            (
                task.investigation_id,
                task.phase.value,
                task.runtime_version,
                task.calendar_started_at,
                task.used_model_calls,
                task.used_tool_calls,
                task.investigation_seconds,
                task.action_seconds,
            ),
        )
        return cursor.rowcount == 1

    async def get_task(self, investigation_id: str) -> InvestigationTask | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM investigation_tasks WHERE investigation_id = %s",
                (investigation_id,),
            )
            row = await cursor.fetchone()
        return _task_from_row(row) if row else None

    async def acquire_task_lease(
        self, investigation_id: str, owner: str, *, seconds: int = 120
    ) -> bool:
        """取得任务租约：同一调查同时只允许一个驱动者。"""
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE investigation_tasks
                   SET lease_owner = %s,
                       lease_expires_at = now() + make_interval(secs => %s),
                       updated_at = now()
                 WHERE investigation_id = %s
                   AND (
                        lease_owner IS NULL
                        OR lease_expires_at IS NULL
                        OR lease_expires_at < now()
                        OR lease_owner = %s
                   )
                """,
                (owner, seconds, investigation_id, owner),
            )
            return cursor.rowcount == 1

    async def release_task_lease(self, investigation_id: str, owner: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                UPDATE investigation_tasks
                   SET lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
                 WHERE investigation_id = %s AND lease_owner = %s
                """,
                (investigation_id, owner),
            )

    async def update_task_usage(
        self,
        investigation_id: str,
        *,
        used_model_calls: int,
        used_tool_calls: int,
        investigation_seconds: float,
        action_seconds: float,
        no_progress_rounds: int | None = None,
    ) -> None:
        """写入绝对用量；分阶段秒数由运行时累计后整体落库。"""
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE investigation_tasks
                   SET used_model_calls = %s,
                       used_tool_calls = %s,
                       investigation_seconds = %s,
                       action_seconds = %s,
                       no_progress_rounds = COALESCE(%s, no_progress_rounds),
                       updated_at = now()
                 WHERE investigation_id = %s
                """,
                (
                    used_model_calls,
                    used_tool_calls,
                    investigation_seconds,
                    action_seconds,
                    no_progress_rounds,
                    investigation_id,
                ),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"任务不存在：{investigation_id}")

    async def set_task_waiting(self, investigation_id: str, *, proposal_id: str) -> None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE investigation_tasks
                   SET phase = 'waiting_approval',
                       proposal_id = %s,
                       paused_at = now(),
                       lease_owner = CASE WHEN EXISTS (
                           SELECT 1 FROM conversations c WHERE c.investigation_id = %s
                       ) THEN lease_owner END,
                       lease_expires_at = CASE WHEN EXISTS (
                           SELECT 1 FROM conversations c WHERE c.investigation_id = %s
                       ) THEN lease_expires_at END,
                       updated_at = now()
                 WHERE investigation_id = %s AND phase <> 'terminal'
                """,
                (proposal_id, investigation_id, investigation_id, investigation_id),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"任务不存在或已结束：{investigation_id}")

    async def resume_task(self, investigation_id: str) -> None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE investigation_tasks
                   SET phase = 'running', paused_at = NULL, updated_at = now()
                 WHERE investigation_id = %s AND phase = 'waiting_approval'
                """,
                (investigation_id,),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"任务不在等待审批：{investigation_id}")

    async def finish_task(
        self,
        investigation_id: str,
        *,
        outcome,
        termination_code: str,
        finished_at,
        recovered_at=None,
        proposal_id: str | None = None,
    ) -> None:
        from aiops.web.lifecycle import _lock_conversation, close_conversation

        async with self._pool.connection() as conn:
            current = await conn.execute(
                "SELECT id FROM conversations WHERE investigation_id = %s", (investigation_id,)
            )
            conversation = await current.fetchone()
            if conversation:
                conversation = await _lock_conversation(conn, conversation["id"])
            cursor = await conn.execute(
                """
                UPDATE investigation_tasks
                   SET phase = 'terminal',
                       outcome = %s,
                       termination_code = %s,
                       finished_at = %s,
                       recovered_at = COALESCE(recovered_at, %s),
                       proposal_id = COALESCE(%s, proposal_id),
                       lease_owner = NULL,
                       lease_expires_at = NULL,
                       updated_at = now()
                 WHERE investigation_id = %s
                """,
                (
                    outcome.value if hasattr(outcome, "value") else outcome,
                    termination_code,
                    finished_at,
                    recovered_at,
                    proposal_id,
                    investigation_id,
                ),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"任务不存在：{investigation_id}")
            if conversation:
                await close_conversation(self, conn, conversation)

    async def record_recovery(self, investigation_id: str, *, at) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                UPDATE investigation_tasks
                   SET recovered_at = COALESCE(recovered_at, %s), updated_at = now()
                 WHERE investigation_id = %s
                """,
                (at, investigation_id),
            )

    async def request_cancel(self, investigation_id: str, *, at) -> bool:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE investigation_tasks
                   SET cancel_requested_at = COALESCE(cancel_requested_at, %s), updated_at = now()
                 WHERE investigation_id = %s AND phase <> 'terminal'
                """,
                (at, investigation_id),
            )
            return cursor.rowcount == 1

    async def has_unfinished_attempt(self, *, non_web_only: bool = False) -> bool:
        """开放监听前检查未决执行，旧表记录按最保守的归属判断。"""
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT 1 FROM action_attempts a
                JOIN action_proposals p ON p.id = a.proposal_id
                LEFT JOIN conversations c ON c.investigation_id = p.investigation_id
                WHERE (a.status IN ('intent', 'executing') OR a.result->>'send_state' = 'sending')
                  AND (NOT %s OR c.id IS NULL)
                LIMIT 1
                """,
                (non_web_only,),
            )
            return await cursor.fetchone() is not None

    async def renew_web_lease(self, investigation_id: str, instance_id: str) -> bool:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE investigation_tasks
                   SET lease_expires_at = clock_timestamp() + interval '300 seconds'
                 WHERE investigation_id = %s AND lease_owner = %s
                   AND lease_expires_at > clock_timestamp() AND phase <> 'terminal'
                """,
                (investigation_id, instance_id),
            )
            return bool(cursor.rowcount)

    async def pending_conversation_hints(self, conversation_id: str) -> list[dict]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT id, content FROM conversation_messages
                 WHERE conversation_id = %s AND delivery = 'received'
                 ORDER BY created_at, id
                """,
                (conversation_id,),
            )
            return await cur.fetchall()

    async def confirm_conversation_hints(self, conversation_id: str, delivered: dict[str, int]):
        async with self._pool.connection() as conn:
            changed = False
            for message_id, round_number in delivered.items():
                cur = await conn.execute(
                    """
                    UPDATE conversation_messages SET delivery = 'delivered', delivered_round = %s
                     WHERE conversation_id = %s AND id = %s AND delivery = 'received'
                    """,
                    (round_number, conversation_id, message_id),
                )
                changed |= bool(cur.rowcount)
            if changed:
                await self._insert_conversation_event(conn, conversation_id, "hint_delivered")

    async def task_has_write_intent(self, investigation_id: str) -> bool:
        """是否存在已发出或结果未知的写意图；用于中断后禁止接管重放。"""
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT 1
                  FROM action_attempts a
                  JOIN action_proposals p ON p.id = a.proposal_id
                 WHERE p.investigation_id = %s
                   AND a.status IN ('intent', 'executing', 'unknown')
                 LIMIT 1
                """,
                (investigation_id,),
            )
            return await cursor.fetchone() is not None

    # ---- 页面会话、请求防重和工作领取 ----

    async def _claim_web_request(
        self,
        conn: psycopg.AsyncConnection,
        request_id: str,
        operation: str,
        resource_id: str | None,
        body: dict,
        response_id: str,
    ) -> WebRequestResult:
        canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        body_hash = hashlib.sha256(canonical.encode()).hexdigest()
        cur = await conn.execute(
            """
            INSERT INTO web_requests (request_id, operation, resource_id, body_hash, response_id)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (request_id) DO NOTHING
            """,
            (request_id, operation, resource_id, body_hash, response_id),
        )
        if cur.rowcount:
            return WebRequestResult(response_id)
        cur = await conn.execute("SELECT * FROM web_requests WHERE request_id = %s", (request_id,))
        saved = await cur.fetchone()
        if saved is None or (saved["operation"], saved["resource_id"], saved["body_hash"]) != (
            operation,
            resource_id,
            body_hash,
        ):
            raise ConversationConflict("idempotency_conflict")
        return WebRequestResult(saved["response_id"], replayed=True)

    async def create_conversation(
        self, request_id: str, incident: Incident, investigation: Investigation
    ) -> WebRequestResult:
        conversation_id = f"conv-{uuid.uuid4().hex}"
        async with self._pool.connection() as conn:
            result = await self._claim_web_request(
                conn,
                request_id,
                "create",
                None,
                {"service": incident.service, "symptoms": incident.symptoms},
                conversation_id,
            )
            if result.replayed:
                return result
            await self._insert_incident(conn, incident)
            await self._insert_investigation(conn, investigation)
            await self._insert_task(
                conn,
                InvestigationTask(
                    investigation_id=investigation.id,
                    phase=TaskPhase.RUNNING,
                    calendar_started_at=investigation.started_at,
                ),
            )
            saved = await conn.execute(
                """
                INSERT INTO conversations (id, investigation_id) VALUES (%s, %s)
                ON CONFLICT ((true)) WHERE finished_at IS NULL DO NOTHING
                """,
                (conversation_id, investigation.id),
            )
            if not saved.rowcount:
                raise ConversationConflict("conversation_active")
            await self._insert_conversation_message(
                conn, conversation_id, f"msg-{uuid.uuid4().hex}", "initial", incident.symptoms
            )
            await conn.execute(
                "INSERT INTO web_work (conversation_id, kind) VALUES (%s, 'start')",
                (conversation_id,),
            )
            return result

    async def _insert_conversation_message(
        self,
        conn: psycopg.AsyncConnection,
        conversation_id: str,
        message_id: str,
        kind: str,
        content: str,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO conversation_messages (id, conversation_id, role, kind, content, delivery)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                message_id,
                conversation_id,
                "agent" if kind == "conclusion" else "user",
                kind,
                content,
                "received" if kind == "hint" else None,
            ),
        )
        await self._insert_conversation_event(conn, conversation_id, "message")

    async def append_conversation_message(
        self, request_id: str, conversation_id: str, content: str
    ) -> WebRequestResult:
        message_id = f"msg-{uuid.uuid4().hex}"
        async with self._pool.connection() as conn:
            result = await self._claim_web_request(
                conn, request_id, "message", conversation_id, {"content": content}, message_id
            )
            if result.replayed:
                return result
            cur = await conn.execute(
                "SELECT finished_at FROM conversations WHERE id = %s FOR UPDATE",
                (conversation_id,),
            )
            row = await cur.fetchone()
            if row is None:
                raise ConversationConflict("not_found")
            if row["finished_at"] is not None:
                raise ConversationConflict("conversation_finished")
            await self._insert_conversation_message(
                conn, conversation_id, message_id, "hint", content
            )
            return result

    async def get_current_conversation(self) -> dict | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM conversations ORDER BY created_at DESC, id DESC LIMIT 1"
            )
            return await cur.fetchone()

    async def _insert_conversation_event(
        self, conn: psycopg.AsyncConnection, conversation_id: str, kind: str
    ) -> None:
        await conn.execute(
            "INSERT INTO conversation_events (conversation_id, kind) VALUES (%s, %s)",
            (conversation_id, kind),
        )

    async def list_conversation_events(
        self, conversation_id: str, *, after: int = 0, limit: int = 100
    ) -> list[dict]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT id, kind, created_at FROM conversation_events
                 WHERE conversation_id = %s AND id > %s
                 ORDER BY id LIMIT %s
                """,
                (conversation_id, after, limit),
            )
            return await cur.fetchall()

    async def claim_web_work(
        self, conversation_id: str, instance_id: str, *, lease_seconds: int = 300
    ) -> dict | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT t.investigation_id, t.phase,
                       lease_owner IS NOT NULL AND lease_expires_at > now() AS leased
                  FROM investigation_tasks t
                  JOIN conversations c USING (investigation_id)
                 WHERE c.id = %s AND c.finished_at IS NULL
                 FOR UPDATE OF t
                """,
                (conversation_id,),
            )
            task = await cur.fetchone()
            if task is None or task["phase"] == "terminal" or task["leased"]:
                return None
            cur = await conn.execute(
                """
                UPDATE web_work
                   SET state = 'running', instance_id = %s, started_at = now()
                 WHERE conversation_id = %s AND state = 'queued'
                 RETURNING *
                """,
                (instance_id, conversation_id),
            )
            work = await cur.fetchone()
            if work is None:
                return None
            await conn.execute(
                """
                UPDATE investigation_tasks
                   SET lease_owner = %s, lease_expires_at = now() + make_interval(secs => %s),
                       updated_at = now()
                 WHERE investigation_id = %s
                """,
                (instance_id, lease_seconds, task["investigation_id"]),
            )
            await self._insert_conversation_event(conn, conversation_id, "state")
            return work | {"investigation_id": task["investigation_id"]}

    # ---- 观测与假设—证据关系 ----

    async def save_observation(self, observation: Observation) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO observations (
                    id, investigation_id, tool_call_id, source, independence_key,
                    summary, raw_ref, status, truncated, delegation_id, time_start, time_end
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (investigation_id, tool_call_id) DO NOTHING
                """,
                (
                    observation.id,
                    observation.investigation_id,
                    observation.tool_call_id,
                    observation.source,
                    observation.independence_key,
                    observation.summary,
                    observation.raw_ref,
                    observation.status.value,
                    observation.truncated,
                    observation.delegation_id,
                    observation.time_range.start,
                    observation.time_range.end,
                ),
            )

    async def get_observation(self, observation_id: str) -> Observation | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM observations WHERE id = %s", (observation_id,)
            )
            row = await cursor.fetchone()
        return _observation_from_row(row) if row else None

    async def get_observation_by_tool_call(
        self, investigation_id: str, tool_call_id: str
    ) -> Observation | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM observations
                 WHERE investigation_id = %s AND tool_call_id = %s
                """,
                (investigation_id, tool_call_id),
            )
            row = await cursor.fetchone()
        return _observation_from_row(row) if row else None

    async def list_observations(self, investigation_id: str) -> list[Observation]:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM observations
                 WHERE investigation_id = %s ORDER BY created_at, id
                """,
                (investigation_id,),
            )
            rows = await cursor.fetchall()
        return [_observation_from_row(row) for row in rows]

    async def upsert_hypothesis_evidence(self, link: HypothesisEvidence) -> None:
        """同一（假设，观测）只保留一条关系，重复标注按最新判断更新。"""
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO hypothesis_evidence (
                    id, investigation_id, hypothesis_id, observation_id, relation, rationale
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (hypothesis_id, observation_id) DO UPDATE SET
                    relation = EXCLUDED.relation,
                    rationale = EXCLUDED.rationale
                """,
                (
                    link.id,
                    link.investigation_id,
                    link.hypothesis_id,
                    link.observation_id,
                    link.relation,
                    link.rationale,
                ),
            )

    async def list_hypothesis_evidence(self, investigation_id: str) -> list[HypothesisEvidence]:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM hypothesis_evidence
                 WHERE investigation_id = %s ORDER BY created_at, id
                """,
                (investigation_id,),
            )
            rows = await cursor.fetchall()
        return [
            HypothesisEvidence(
                id=row["id"],
                investigation_id=row["investigation_id"],
                hypothesis_id=row["hypothesis_id"],
                observation_id=row["observation_id"],
                relation=row["relation"],
                rationale=row["rationale"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    # ---- 技能与委派调查 ----

    async def set_investigation_skills(
        self, investigation_id: str, skills: Sequence[SkillUsage]
    ) -> None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "UPDATE investigations SET skills = %s WHERE id = %s",
                (Jsonb([item.model_dump(mode="json") for item in skills]), investigation_id),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"调查不存在：{investigation_id}")

    async def create_delegation(self, delegation: Delegation) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO delegations (
                    id, investigation_id, objective, status, result_summary,
                    skills, started_at, finished_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    delegation.id,
                    delegation.investigation_id,
                    delegation.objective,
                    delegation.status.value,
                    delegation.result_summary,
                    delegation.skills,
                    delegation.started_at,
                    delegation.finished_at,
                ),
            )

    async def finish_delegation(
        self,
        delegation_id: str,
        *,
        status: DelegationStatus,
        result_summary: str,
        skills: Sequence[str],
        finished_at: datetime,
    ) -> None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE delegations
                   SET status = %s, result_summary = %s, skills = %s, finished_at = %s
                 WHERE id = %s
                """,
                (status.value, result_summary[:2000], list(skills), finished_at, delegation_id),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"委派调查不存在：{delegation_id}")

    async def list_delegations(self, investigation_id: str) -> list[Delegation]:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM delegations WHERE investigation_id = %s ORDER BY started_at, id",
                (investigation_id,),
            )
            rows = await cursor.fetchall()
        return [_delegation_from_row(row) for row in rows]

    async def count_failed_delegations(self, investigation_id: str) -> int:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT count(*) AS failed_count FROM delegations "
                "WHERE investigation_id = %s AND status = %s",
                (investigation_id, DelegationStatus.FAILED.value),
            )
            row = await cursor.fetchone()
        return int(row["failed_count"]) if row else 0

    # ---- 工具请求账本 ----

    async def start_tool_request(self, request: ToolRequest) -> bool:
        """登记一次工具请求；同一 (investigation, tool_call_id) 只允许一个驱动者。"""
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO tool_requests (
                    investigation_id, tool_call_id, tool, arguments, state, attempt, started_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (investigation_id, tool_call_id) DO NOTHING
                """,
                (
                    request.investigation_id,
                    request.tool_call_id,
                    request.tool,
                    Jsonb(request.arguments),
                    request.state.value,
                    request.attempt,
                    request.started_at,
                ),
            )
            return cursor.rowcount == 1

    async def get_tool_request(
        self, investigation_id: str, tool_call_id: str
    ) -> ToolRequest | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM tool_requests
                 WHERE investigation_id = %s AND tool_call_id = %s
                """,
                (investigation_id, tool_call_id),
            )
            row = await cursor.fetchone()
        return _tool_request_from_row(row) if row else None

    async def finish_tool_request(
        self,
        investigation_id: str,
        tool_call_id: str,
        *,
        state: ToolRequestState,
        result=None,
        result_ref: str | None = None,
        error_code: str | None = None,
        proposal_id: str | None = None,
        action_attempt_id: str | None = None,
    ) -> None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE tool_requests
                   SET state = %s,
                       result = COALESCE(%s, result),
                       result_ref = COALESCE(%s, result_ref),
                       error_code = COALESCE(%s, error_code),
                       proposal_id = COALESCE(%s, proposal_id),
                       action_attempt_id = COALESCE(%s, action_attempt_id),
                       finished_at = now(),
                       updated_at = now()
                 WHERE investigation_id = %s AND tool_call_id = %s
                """,
                (
                    state.value,
                    Jsonb(result) if result is not None else None,
                    result_ref,
                    error_code,
                    proposal_id,
                    action_attempt_id,
                    investigation_id,
                    tool_call_id,
                ),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"工具请求不存在：{tool_call_id}")

    async def list_tool_requests(self, investigation_id: str) -> list[ToolRequest]:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM tool_requests
                 WHERE investigation_id = %s ORDER BY started_at, tool_call_id
                """,
                (investigation_id,),
            )
            rows = await cursor.fetchall()
        return [_tool_request_from_row(row) for row in rows]

    async def count_attempts_for_investigation(self, investigation_id: str) -> int:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT count(*) AS total
                  FROM action_attempts a
                  JOIN action_proposals p ON p.id = a.proposal_id
                 WHERE p.investigation_id = %s
                """,
                (investigation_id,),
            )
            row = await cursor.fetchone()
        return int(row["total"]) if row else 0

    async def get_attempt_by_id(self, attempt_id: str) -> ActionAttempt | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM action_attempts WHERE id = %s", (attempt_id,)
            )
            row = await cursor.fetchone()
        return _attempt_from_row(row) if row else None


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
        purpose=row.get("purpose") or "remediation",
        hypothesis_id=row.get("hypothesis_id"),
        prediction=row.get("prediction") or "",
        observation_conditions=row.get("observation_conditions") or {},
        observation_window_seconds=row.get("observation_window_seconds"),
    )


def _hypothesis_from_row(row: dict) -> Hypothesis:
    return Hypothesis(
        id=row["id"],
        investigation_id=row["investigation_id"],
        parent_id=row["parent_id"],
        statement=row["statement"],
        origin=row["origin"],
        status=row["status"],
        update_reason=row.get("update_reason") or "",
    )


def _observation_from_row(row: dict) -> Observation:
    return Observation(
        id=row["id"],
        investigation_id=row["investigation_id"],
        tool_call_id=row["tool_call_id"],
        source=row["source"],
        independence_key=row["independence_key"],
        summary=row["summary"],
        raw_ref=row["raw_ref"] or "",
        status=row["status"],
        truncated=bool(row["truncated"]),
        delegation_id=row.get("delegation_id"),
        time_range=TimeRange(start=row["time_start"], end=row["time_end"]),
        created_at=row.get("created_at"),
    )


def _delegation_from_row(row: dict) -> Delegation:
    return Delegation(
        id=row["id"],
        investigation_id=row["investigation_id"],
        objective=row["objective"],
        status=DelegationStatus(row["status"]),
        result_summary=row["result_summary"] or "",
        skills=list(row["skills"] or []),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        created_at=row.get("created_at"),
    )


def _task_from_row(row: dict) -> InvestigationTask:
    return InvestigationTask(
        investigation_id=row["investigation_id"],
        phase=TaskPhase(row["phase"]),
        outcome=row["outcome"],
        termination_code=row["termination_code"],
        proposal_id=row["proposal_id"],
        ready_proposal_id=row["ready_proposal_id"],
        actions_disabled=row["actions_disabled"],
        runtime_version=row["runtime_version"],
        used_model_calls=row["used_model_calls"],
        used_tool_calls=row["used_tool_calls"],
        investigation_seconds=float(row["investigation_seconds"]),
        action_seconds=float(row["action_seconds"]),
        no_progress_rounds=row["no_progress_rounds"],
        calendar_started_at=row["calendar_started_at"],
        paused_at=row["paused_at"],
        finished_at=row["finished_at"],
        cancel_requested_at=row["cancel_requested_at"],
        recovered_at=row["recovered_at"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        updated_at=row["updated_at"],
    )


def _tool_request_from_row(row: dict) -> ToolRequest:
    return ToolRequest(
        investigation_id=row["investigation_id"],
        tool_call_id=row["tool_call_id"],
        tool=row["tool"],
        arguments=row["arguments"] or {},
        state=row["state"],
        result=row["result"],
        proposal_id=row["proposal_id"],
        action_attempt_id=row["action_attempt_id"],
        error_code=row["error_code"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
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


def _followup_from_row(row: dict) -> ActionFollowup:
    return ActionFollowup(
        id=row["id"],
        proposal_id=row["proposal_id"],
        attempt_id=row["attempt_id"],
        cluster=row["cluster"],
        namespace=row["namespace"],
        target_kind=row["target_kind"],
        target_name=row["target_name"],
        outcome=AttemptOutcome(row["outcome"]),
        reason=row["reason"],
        evidence=row["evidence"] or {},
        status=FollowupStatus(row["status"]),
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
        resolved_by=row["resolved_by"],
        resolution=row["resolution"],
    )
