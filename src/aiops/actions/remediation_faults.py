"""两个触发入口共用的目标故障登记和恢复条件。"""

from collections.abc import Iterable
from datetime import datetime
from uuid import uuid4

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from aiops.actions.remediation_alerts import fault_is_not_clear
from aiops.actions.safety import _one
from aiops.contracts import ActionTarget
from aiops.inspection.models import Check, InspectionRun, Observation


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


async def save_inspection_signal(
    conn: AsyncConnection,
    run_id: str,
    fault_id: str,
    checks: list[Check],
    observations: Iterable[Observation],
    abnormal_at: datetime | None,
    healthy_at: datetime | None,
) -> None:
    await conn.execute(
        """
        INSERT INTO remediation_inspection_signals
            (fault_id, run_id, required_checks, observation_ids, abnormal_at, healthy_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (fault_id, run_id) DO UPDATE SET
            abnormal_at = GREATEST(
                remediation_inspection_signals.abnormal_at, EXCLUDED.abnormal_at),
            healthy_at = CASE WHEN COALESCE(remediation_inspection_signals.abnormal_at,
                EXCLUDED.abnormal_at) IS NULL THEN EXCLUDED.healthy_at END,
            observation_ids = ARRAY(
                SELECT DISTINCT unnest(
                    remediation_inspection_signals.observation_ids || EXCLUDED.observation_ids
                )
            ),
            required_checks = EXCLUDED.required_checks
        """,
        (
            fault_id,
            run_id,
            Jsonb([check.model_dump(mode="json", exclude={"ordinal"}) for check in checks]),
            [item.id for item in observations],
            abnormal_at,
            healthy_at,
        ),
    )


async def record_abnormal_observation(
    conn: AsyncConnection, run: InspectionRun, observation: Observation
) -> None:
    # 调用方已按名称、会话和轮次顺序加锁。就绪检查提供 UID，业务指标沿用本轮身份。
    target = observation.target
    uid = observation.evidence.get("uid")
    if observation.check_id == "workload-signal":
        readiness = await _one(
            conn,
            """
            SELECT evidence->>'uid' AS uid FROM inspection_observations
            WHERE run_id = %s AND target = %s AND check_id = 'workload-readiness'
            ORDER BY recorded_order DESC LIMIT 1
            """,
            (run.id, Jsonb(target)),
        )
        uid = readiness["uid"] if readiness else None
    if not uid:
        existing = await _one(
            conn,
            """
            SELECT workload_uid FROM remediation_faults
            WHERE (cluster, namespace, workload_kind, workload_name) = (%s, %s, %s, %s)
                AND cleared_at IS NULL
            ORDER BY opened_at DESC LIMIT 1
            """,
            (run.cluster, target["namespace"], target["kind"], target["name"]),
        )
        if existing is None:
            return
        uid = existing["workload_uid"]
    workload = ActionTarget(cluster=run.cluster, **target, uid=uid, resource_version="")
    await lock_fault_uid(conn, workload)
    fault = await active_fault(conn, workload)
    if fault is None or await signal_is_late(conn, workload, observation.sampled_at):
        return
    await save_inspection_signal(
        conn,
        run.id,
        fault["id"],
        [check for check in run.checks if check.target == target],
        (observation,),
        observation.sampled_at,
        None,
    )


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
        SELECT latest.*, (
            SELECT max(s.abnormal_at) FROM remediation_inspection_signals s
            WHERE s.fault_id = latest.fault_id
        ) AS last_abnormal_at FROM latest
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
        or (health["last_abnormal_at"] is not None and health["last_abnormal_at"] >= window_start)
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
