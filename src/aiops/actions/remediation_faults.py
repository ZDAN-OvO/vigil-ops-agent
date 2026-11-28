"""两个触发入口共用的目标故障登记和恢复条件。"""

from datetime import datetime
from uuid import uuid4

from psycopg import AsyncConnection

from aiops.actions.remediation_alerts import fault_is_not_clear
from aiops.actions.safety import _one
from aiops.contracts import ActionTarget


def identity(target: ActionTarget) -> tuple[str, str, str, str]:
    return target.cluster, target.namespace, target.kind, target.name


async def lock_fault_uid(conn: AsyncConnection, target: ActionTarget) -> None:
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 1))",
        (f"{target.cluster}/{target.namespace}/{target.uid}",),
    )


async def active_fault(conn: AsyncConnection, target: ActionTarget) -> dict | None:
    return await _one(
        conn,
        """
        SELECT * FROM remediation_faults
        WHERE (cluster, namespace, workload_uid) = (%s, %s, %s)
            AND cleared_at IS NULL FOR UPDATE
        """,
        (target.cluster, target.namespace, target.uid),
    )


async def lock_fault(conn: AsyncConnection, target: ActionTarget, fault_id: str) -> dict | None:
    await lock_fault_uid(conn, target)
    return await _one(
        conn,
        """
        SELECT * FROM remediation_faults WHERE id = %s
            AND (cluster, namespace, workload_uid) = (%s, %s, %s) FOR UPDATE
        """,
        (fault_id, target.cluster, target.namespace, target.uid),
    )


async def previous_uid_blocks(conn: AsyncConnection, target: ActionTarget) -> bool:
    # 名称锁保护同名重建。旧 UID 的活动故障和未核实写入继续占用目标。
    return (
        await _one(
            conn,
            """
        SELECT 1 FROM remediation_faults f
        WHERE (f.cluster, f.namespace, f.workload_kind, f.workload_name) = (%s, %s, %s, %s)
          AND f.workload_uid <> %s
          AND (f.cleared_at IS NULL OR EXISTS (
              SELECT 1 FROM remediation_jobs j
              JOIN action_proposals p ON p.id = j.proposal_id
              LEFT JOIN action_attempts a ON a.proposal_id = j.proposal_id
              WHERE j.fault_id = f.id AND j.sent_at IS NOT NULL
                AND (j.state <> 'finished' OR p.status = 'unknown'
                     OR a.finished_at IS NULL OR a.status = 'unknown')
          ))
        LIMIT 1
        """,
            (*identity(target), target.uid),
        )
        is not None
    )


async def signal_is_late(conn: AsyncConnection, target: ActionTarget, signal_at: datetime) -> bool:
    return (
        await _one(
            conn,
            """
        SELECT 1 FROM remediation_faults
        WHERE (cluster, namespace) = (%s, %s)
            AND ((workload_kind, workload_name) = (%s, %s) OR workload_uid = %s)
            AND cleared_at > %s LIMIT 1
        """,
            (*identity(target), target.uid, signal_at),
        )
        is not None
    )


async def create_fault(
    conn: AsyncConnection, target: ActionTarget, *, opened_at: datetime | None = None
) -> str:
    identifier = uuid4().hex
    await conn.execute(
        """
        INSERT INTO remediation_faults
            (id, cluster, namespace, workload_kind, workload_name, workload_uid, opened_at)
        VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s, clock_timestamp()))
        """,
        (identifier, *identity(target), target.uid, opened_at),
    )
    return identifier


async def has_inspection_signal(conn: AsyncConnection, fault_id: str) -> bool:
    return (
        await _one(
            conn,
            "SELECT 1 FROM remediation_inspection_signals "
            "WHERE fault_id = %s AND abnormal_at IS NOT NULL LIMIT 1",
            (fault_id,),
        )
        is not None
    )


async def health_signal(conn: AsyncConnection, fault_id: str) -> dict | None:
    """最新一轮完整健康，且所有登记检查的样本晚于该故障最后一次异常。"""
    return await _one(
        conn,
        """
        WITH latest AS (
            SELECT s.* FROM remediation_inspection_signals s
            JOIN inspection_runs r ON r.id = s.run_id
            WHERE s.fault_id = %s
            ORDER BY r.scheduled_at DESC LIMIT 1
        )
        SELECT latest.* FROM latest
        WHERE latest.healthy_at IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM remediation_inspection_signals s
            WHERE s.fault_id = latest.fault_id AND s.abnormal_at IS NOT NULL
                AND (s.abnormal_at >= latest.healthy_at
                     OR NOT latest.required_checks @> s.required_checks)
        )
        """,
        (fault_id,),
    )


async def jobs_settled(conn: AsyncConnection, fault_id: str) -> dict:
    return await _one(
        conn,
        """
        SELECT max(a.finished_at) AS finished_at,
            COALESCE(bool_and(j.state = 'finished' AND p.status <> 'unknown'
                AND (j.sent_at IS NULL OR (a.finished_at IS NOT NULL AND a.status <> 'unknown'))),
                true) AS settled,
            (SELECT max(j2.effective_at) FROM remediation_jobs j2
             WHERE j2.fault_id = %s) AS effective_at
        FROM remediation_jobs j
        JOIN action_proposals p ON p.id = j.proposal_id
        LEFT JOIN action_attempts a ON a.proposal_id = j.proposal_id
        WHERE j.fault_id = %s
        """,
        (fault_id, fault_id),
    )


async def inspection_can_clear(
    conn: AsyncConnection,
    fault: dict,
    *,
    window_start: datetime,
    window_end: datetime,
    now: datetime,
) -> bool:
    # 进入时已按名称、会话、UID、故障行加锁。
    health = await health_signal(conn, fault["id"])
    jobs = await jobs_settled(conn, fault["id"])
    if (
        fault["cleared_at"] is not None
        or health is None
        or window_end > now
        or not jobs["settled"]
        or fault["opened_at"] > window_start
        or (jobs["effective_at"] is not None and jobs["effective_at"] > window_start)
        or any(
            (now - health["healthy_at"]).total_seconds() > check["max_age_seconds"]
            for check in health["required_checks"]
        )
        or await _one(
            conn,
            """
            SELECT 1 FROM action_locks l WHERE l.target_key = %s OR l.target_key IN (
                SELECT unnest(j.resource_keys) FROM remediation_jobs j WHERE j.fault_id = %s
            ) LIMIT 1
            """,
            (
                "/".join(
                    fault[key]
                    for key in (
                        "cluster",
                        "namespace",
                        "workload_kind",
                        "workload_name",
                    )
                ),
                fault["id"],
            ),
        )
        is not None
        or (
            jobs["finished_at"] is not None
            and health["healthy_at"] <= jobs["finished_at"]
            and (jobs["effective_at"] is None or health["healthy_at"] <= jobs["effective_at"])
        )
        or await fault_is_not_clear(
            conn, fault_id=fault["id"], started_at=window_start, recovered_by=window_start
        )
    ):
        return False
    return True
