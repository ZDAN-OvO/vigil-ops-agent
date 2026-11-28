"""保存告警发作身份。恢复只结算同一个指纹和开始时间。"""

from datetime import datetime
from typing import Any
from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.types.json import Jsonb

from aiops.contracts import ActionTarget
from aiops.db import Store

# 已绑定的告警归属固定。未绑定的告警必须始于上次结案后，且与本次故障重叠。
_UNLINKED_FAULT_ALERT = sql.SQL("""
    a.fault_id IS NULL
    AND a.labels->>'namespace' = f.namespace
    AND a.labels->>'target' = f.workload_name
    AND COALESCE(a.labels->>'workload_kind', f.workload_kind) = f.workload_kind
    AND a.starts_at >= COALESCE((
        SELECT max(previous.cleared_at) FROM remediation_faults previous
        WHERE (previous.cluster, previous.namespace) = (f.cluster, f.namespace)
          AND (previous.workload_uid = f.workload_uid
               OR (previous.workload_kind, previous.workload_name) =
                  (f.workload_kind, f.workload_name))
          AND previous.id <> f.id AND previous.cleared_at <= f.opened_at
    ), '-infinity'::timestamptz)
    AND (a.resolved_at IS NULL OR GREATEST(a.starts_at, a.resolved_at) >= LEAST(f.opened_at, (
        SELECT min(linked.starts_at) FROM remediation_alerts linked
        WHERE linked.fault_id = f.id
    )))
""")


async def record_firing(
    conn: AsyncConnection,
    *,
    cluster: str,
    group_key: str,
    fingerprint: str,
    starts_at: datetime,
    labels: dict[str, str],
) -> bool:
    inserted = await conn.execute(
        """
        INSERT INTO remediation_alerts (cluster, group_key, fingerprint, starts_at, labels)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (cluster, group_key, fingerprint, starts_at) DO NOTHING
        """,
        (cluster, group_key, fingerprint, starts_at, Jsonb(labels)),
    )
    return inserted.rowcount == 1


async def link_firing(
    conn: AsyncConnection,
    *,
    cluster: str,
    group_key: str,
    fingerprint: str,
    starts_at: datetime,
    session_id: str,
    evaluate: bool,
) -> None:
    await conn.execute(
        """
        UPDATE remediation_alerts SET session_id = %s,
            evaluated_at = CASE WHEN %s THEN NULL ELSE clock_timestamp() END
        WHERE cluster = %s AND group_key = %s AND fingerprint = %s AND starts_at = %s
        """,
        (session_id, evaluate, cluster, group_key, fingerprint, starts_at),
    )


async def record_resolved(
    conn: AsyncConnection,
    *,
    cluster: str,
    group_key: str,
    fingerprint: str,
    starts_at: datetime,
    labels: dict[str, str],
) -> None:
    # 先到的恢复通知也要留下记录，阻止随后到达的陈旧 firing 获得新名额。
    await conn.execute(
        """
        INSERT INTO remediation_alerts
            (cluster, group_key, fingerprint, starts_at, labels, resolved_at, evaluated_at)
        VALUES (%s, %s, %s, %s, %s, clock_timestamp(), clock_timestamp())
        ON CONFLICT (cluster, group_key, fingerprint, starts_at)
        DO UPDATE SET resolved_at = COALESCE(
            remediation_alerts.resolved_at, EXCLUDED.resolved_at
        )
        """,
        (cluster, group_key, fingerprint, starts_at, Jsonb(labels)),
    )


async def clearance_state(conn: AsyncConnection, session_id: str) -> tuple[bool, bool, bool]:
    row = await (
        await conn.execute(
            """
            SELECT
                EXISTS (SELECT 1 FROM remediation_alerts WHERE session_id = %(session)s)
                    AS known,
                EXISTS (SELECT 1 FROM remediation_alerts WHERE session_id = %(session)s
                        AND resolved_at IS NULL) AS firing,
                EXISTS (SELECT 1 FROM remediation_alerts
                        WHERE session_id = %(session)s AND evaluated_at IS NULL)
                OR EXISTS (
                    SELECT 1 FROM remediation_jobs j JOIN action_proposals p
                        ON p.id = j.proposal_id WHERE p.session_id = %(session)s
                    AND (j.state IN ('ready', 'sending', 'applying', 'observing')
                        OR (j.sent_at IS NOT NULL AND p.status <> 'succeeded'
                            AND NOT EXISTS (
                                SELECT 1 FROM remediation_faults f
                                WHERE f.id = j.fault_id AND f.cleared_at IS NOT NULL
                            )))
                ) AS blocked
            """,
            {"session": session_id},
        )
    ).fetchone()
    return row["known"], row["firing"], row["blocked"]


async def claim_alert(store: Store) -> dict[str, Any] | None:
    async with store.transaction() as conn:
        cursor = await conn.execute(
            """
            WITH next_alert AS (
                SELECT cluster, group_key, fingerprint, starts_at
                FROM remediation_alerts WHERE evaluated_at IS NULL
                  AND session_id IS NOT NULL AND evaluate_after <= clock_timestamp()
                  AND (evaluation_expires_at IS NULL OR evaluation_expires_at <= clock_timestamp())
                ORDER BY evaluate_after, starts_at, fingerprint
                LIMIT 1 FOR UPDATE SKIP LOCKED
            )
            UPDATE remediation_alerts a SET evaluation_owner = %s,
                evaluation_expires_at = clock_timestamp() + interval '120 seconds'
            FROM next_alert q WHERE (a.cluster, a.group_key, a.fingerprint, a.starts_at) =
                (q.cluster, q.group_key, q.fingerprint, q.starts_at)
            RETURNING a.*
            """,
            (uuid4().hex,),
        )
        return await cursor.fetchone()


def alert_key(alert: dict[str, Any]) -> tuple[str, str, str, datetime]:
    return alert["cluster"], alert["group_key"], alert["fingerprint"], alert["starts_at"]


async def complete_alert(
    conn: AsyncConnection,
    alert: dict[str, Any],
    *,
    fault_id: str | None = None,
    retry_seconds: int | None = None,
) -> bool:
    cursor = await conn.execute(
        """
        UPDATE remediation_alerts SET fault_id = COALESCE(%s, fault_id),
            evaluated_at = CASE WHEN %s::int IS NULL THEN clock_timestamp() END,
            evaluate_after = clock_timestamp() + COALESCE(%s::int, 0) * interval '1 second',
            evaluation_owner = NULL, evaluation_expires_at = NULL
        WHERE (cluster, group_key, fingerprint, starts_at) = (%s, %s, %s, %s)
          AND evaluation_owner = %s AND evaluation_expires_at > clock_timestamp()
        """,
        (fault_id, retry_seconds, retry_seconds, *alert_key(alert), alert["evaluation_owner"]),
    )
    return cursor.rowcount == 1


async def lock_workload_alerts(
    conn: AsyncConnection,
    cluster: str,
    namespace: str,
    target: str,
) -> None:
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 2))",
        (f"{cluster}/{namespace}/{target}",),
    )


async def recovery_candidate(
    conn: AsyncConnection,
    target: ActionTarget,
    *,
    next_start: datetime,
) -> dict[str, Any] | None:
    """找到已用过 AUTO、此前告警均已结束且没有悬而未决写入的故障。"""
    return await (
        await conn.execute(
            sql.SQL("""
            SELECT f.id AS fault_id, original.proposal_id,
                original.probe,
                GREATEST(f.opened_at, alerts.resolved_at, jobs.finished_at) AS earliest_start
            FROM remediation_faults f
            JOIN LATERAL (
                SELECT j.proposal_id, p.observation_conditions->'verification_plan' AS probe
                FROM remediation_jobs j JOIN action_proposals p ON p.id = j.proposal_id
                WHERE j.fault_id = f.id AND j.automated AND j.sent_at IS NOT NULL
                    AND j.parent_proposal_id IS NULL
            ) original ON TRUE
            CROSS JOIN LATERAL (
                SELECT max(a.resolved_at) AS resolved_at,
                    bool_and(a.starts_at < %(next_start)s AND a.resolved_at IS NOT NULL
                        AND a.resolved_at < %(next_start)s) AS settled,
                    bool_or(a.fault_id = f.id) AS has_linked_alert
                FROM remediation_alerts a
                WHERE a.cluster = f.cluster AND (
                    a.fault_id = f.id OR (a.starts_at < %(next_start)s AND {})
                )
            ) alerts
            CROSS JOIN LATERAL (
                SELECT max(a.finished_at) AS finished_at,
                    bool_and(j.state = 'finished' AND p.status <> 'unknown'
                        AND (j.sent_at IS NULL OR (
                            a.finished_at IS NOT NULL AND a.status <> 'unknown'
                        ))
                    ) AS settled
                FROM remediation_jobs j
                JOIN action_proposals p ON p.id = j.proposal_id
                LEFT JOIN action_attempts a ON a.proposal_id = j.proposal_id
                WHERE j.fault_id = f.id
            ) jobs
            WHERE (f.cluster, f.namespace, f.workload_uid) = (%(cluster)s, %(namespace)s, %(uid)s)
                AND f.cleared_at IS NULL AND f.opened_at < %(next_start)s
                AND original.probe IS NOT NULL AND alerts.settled AND alerts.has_linked_alert
                AND jobs.settled
                AND NOT EXISTS (
                    SELECT 1 FROM remediation_inspection_signals s
                    WHERE s.fault_id = f.id AND s.abnormal_at IS NOT NULL
                )
                AND NOT EXISTS (
                    SELECT 1 FROM remediation_jobs j
                    JOIN action_locks l ON l.target_key = ANY(j.resource_keys)
                    WHERE j.fault_id = f.id
                )
            """).format(_UNLINKED_FAULT_ALERT),
            {
                "cluster": target.cluster,
                "namespace": target.namespace,
                "uid": target.uid,
                "next_start": next_start,
            },
        )
    ).fetchone()


async def fault_is_not_clear(
    conn: AsyncConnection,
    *,
    fault_id: str,
    started_at: datetime,
    recovered_by: datetime | None = None,
) -> bool:
    row = await (
        await conn.execute(
            sql.SQL("""
            SELECT 1 FROM remediation_faults f WHERE f.id = %(fault)s AND (
                EXISTS (
                    SELECT 1 FROM remediation_alerts a WHERE a.cluster = f.cluster
                    AND (a.fault_id = f.id OR ({}))
                    AND (a.resolved_at IS NULL OR a.starts_at >= %(start)s
                         OR a.resolved_at > %(recovered)s::timestamptz)
                ) OR EXISTS (
                    SELECT 1 FROM remediation_inspection_signals s
                    WHERE s.fault_id = f.id AND s.abnormal_at >= %(start)s
                )
            )
            """).format(_UNLINKED_FAULT_ALERT),
            {"fault": fault_id, "start": started_at, "recovered": recovered_by},
        )
    ).fetchone()
    return row is not None
