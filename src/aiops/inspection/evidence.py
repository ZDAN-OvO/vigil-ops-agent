"""巡检授权使用已登记、已落账的观察。"""

from datetime import datetime
from typing import Any

from psycopg import AsyncConnection

from aiops.inspection.models import Check, InspectionRun, Observation

WORKLOAD_CHECKS = frozenset({"workload-readiness", "workload-signal"})
OBSERVATION_FIELDS = ("value", "desired_replicas", "ready_replicas")


def fresh(
    observation: Observation, check: Check, now: datetime, *, max_age: int | None = None
) -> bool:
    timestamp = observation.sampled_at
    age_limit = min(check.max_age_seconds, max_age or check.max_age_seconds)
    return (
        observation.ordinal == check.ordinal
        and observation.check_id == check.check_id
        and observation.target == check.target
        and observation.status in {"healthy", "abnormal"}
        and timestamp is not None
        and 0 <= (now - timestamp).total_seconds() <= age_limit
    )


def values(observation: Observation) -> dict[str, Any]:
    return {
        key: observation.evidence[key] for key in OBSERVATION_FIELDS if key in observation.evidence
    }


async def latest_target_observations(
    conn: AsyncConnection, run_id: str, target: dict[str, str]
) -> dict[str, Observation]:
    from psycopg.types.json import Jsonb

    rows = await conn.execute(
        """
        SELECT DISTINCT ON (ordinal) id, ordinal, check_id, target, status, reason,
            sampled_at, evidence, evidence_refs
        FROM inspection_observations WHERE run_id = %s AND target = %s
        ORDER BY ordinal, recorded_order DESC
        """,
        (run_id, Jsonb(target)),
    )
    return {row["check_id"]: Observation(**row) for row in await rows.fetchall()}


def review_samples(
    run: InspectionRun,
    observations: dict[str, Observation],
    *,
    uid: str,
    trigger: str,
    required: set[str],
    max_age: int,
    now: datetime,
) -> tuple[Observation, ...]:
    readiness = observations.get("workload-readiness")
    if readiness is None or readiness.evidence.get("uid") != uid:
        return ()
    selected = {name: observations.get(name) for name in required}
    checks = {check.check_id: check for check in run.checks if check.target == readiness.target}
    if (
        trigger not in selected
        or selected[trigger] is None
        or selected[trigger].status != "abnormal"
        or any(
            item is None
            or name not in checks
            or not fresh(item, checks[name], now, max_age=max_age)
            for name, item in selected.items()
        )
    ):
        return ()
    return tuple(sorted(selected.values(), key=lambda item: item.ordinal))
