"""保留的发布历史与已核实回滚，以及只读 PostgreSQL 适配器。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

import psycopg
from psycopg.rows import dict_row

from aiops.monitoring.signals import as_time, block, iso

HISTORY_WINDOW = timedelta(hours=24)
REVISION = "deployment.kubernetes.io/revision"
REVISION_HISTORY = "deployment.kubernetes.io/revision-history"
CHANGE_CAUSE = "kubernetes.io/change-cause"
VIGIL_ROLLBACK_CAUSE = "vigil rollback "


@dataclass(frozen=True)
class RollbackRecord:
    proposal_id: str
    deployment_uid: str
    service: str
    target_revision: int
    target_image: str
    started_at: datetime
    approved_by: str | None
    finished_at: datetime | None = None


class RollbackHistory(Protocol):
    async def __call__(self, start: datetime, end: datetime) -> list[RollbackRecord]: ...


class PostgresRollbackHistory:
    """读取现有动作账本的已完成记录。加载与迁移由 Vigil 管理。"""

    def __init__(self, dsn: str, *, cluster: str, namespace: str) -> None:
        self._dsn = dsn
        self._cluster = cluster
        self._namespace = namespace

    async def __call__(self, start: datetime, end: datetime) -> list[RollbackRecord]:
        async with await psycopg.AsyncConnection.connect(
            self._dsn, row_factory=dict_row, connect_timeout=5
        ) as conn:
            await conn.execute("SET TRANSACTION READ ONLY")
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            cursor = await conn.execute(
                """
                SELECT p.id AS proposal_id, p.target_uid AS deployment_uid,
                       p.target_name AS service,
                       COALESCE(a.snapshot->>'to_revision', p.parameters->>'to_revision')
                           AS target_revision,
                       COALESCE(a.snapshot->>'target_image', p.parameters->>'target_image')
                           AS target_image,
                       a.started_at, a.finished_at, approval.actor AS approved_by
                  FROM action_attempts a
                  JOIN action_proposals p ON p.id = a.proposal_id
                  LEFT JOIN LATERAL (
                      SELECT actor FROM approvals r
                       WHERE r.proposal_id = p.id AND r.decision = 'approved'
                         AND r.decided_at <= a.started_at
                         AND (r.expires_at IS NULL OR r.expires_at >= a.started_at)
                         AND (a.snapshot->>'approval_hash' IS NULL
                              OR r.proposal_hash = a.snapshot->>'approval_hash')
                       ORDER BY r.decided_at DESC, r.id DESC LIMIT 1
                  ) approval ON true
                 WHERE p.cluster = %s AND p.namespace = %s AND p.target_kind = 'Deployment'
                   AND p.kind = 'rollout_undo' AND p.status = 'succeeded'
                   AND a.status = 'verified' AND a.finished_at <= %s
                   AND a.started_at >= %s AND a.finished_at >= a.started_at
                """,
                (self._cluster, self._namespace, end, start),
            )
            return [
                RollbackRecord(**(row | {"target_revision": int(row["target_revision"])}))
                for row in await cursor.fetchall()
            ]


def controller_uid(item: dict, kind: str) -> str | None:
    for owner in item["metadata"].get("owner_references") or []:
        if owner.get("controller") is True and owner.get("kind") == kind:
            return owner.get("uid")
    return None


@dataclass(frozen=True)
class Revision:
    service: str
    deployment_uid: str
    created_at: datetime
    revisions: tuple[int, ...]
    image: str | None
    cause: str


def _revisions(deployments: list[dict], replicasets: list[dict]) -> tuple[list[Revision], bool]:
    owners = {item["metadata"]["uid"]: item["metadata"]["name"] for item in deployments}
    revisions = []
    incomplete = False
    for item in replicasets:
        uid = controller_uid(item, "Deployment")
        if uid is None or uid not in owners:
            continue
        metadata = item["metadata"]
        annotations = metadata.get("annotations") or {}
        created_at = as_time(metadata.get("creation_timestamp"))
        current = annotations.get(REVISION)
        versions = (annotations.get(REVISION_HISTORY) or "").split(",") + [current]
        if not created_at or not current or any(n and not n.isdecimal() for n in versions):
            incomplete = True
            continue
        containers = item["spec"]["template"]["spec"].get("containers") or []
        revisions.append(
            Revision(
                service=owners[uid],
                deployment_uid=uid,
                created_at=created_at,
                revisions=tuple(int(n) for n in versions if n),
                image=containers[0]["image"] if containers else None,
                cause=annotations.get(CHANGE_CAUSE) or "",
            )
        )
    return revisions, incomplete


def _retained_revision(record: RollbackRecord, revisions: list[Revision]) -> Revision | None:
    same_template = [
        item
        for item in revisions
        if item.deployment_uid == record.deployment_uid
        and item.service == record.service
        and item.image == record.target_image
    ]
    exact = [
        item
        for item in same_template
        if item.cause == VIGIL_ROLLBACK_CAUSE + record.proposal_id
        and (record.target_revision in item.revisions or _created_during(item, record))
    ]
    candidates = exact or [
        item
        for item in same_template
        if record.target_revision in item.revisions and item.created_at <= record.started_at
    ]
    return candidates[0] if len(candidates) == 1 else None


def _created_during(revision: Revision, record: RollbackRecord) -> bool:
    return (
        record.finished_at is not None
        and record.started_at <= revision.created_at <= record.finished_at
    )


def changes(
    namespace: str,
    deployments: list[dict] | None,
    replicasets: list[dict] | None,
    rollbacks: list[RollbackRecord] | None,
    now: datetime,
) -> dict:
    failed = [
        name
        for name, result in (
            ("deployments", deployments),
            ("replicasets", replicasets),
            ("rollbacks", rollbacks),
        )
        if result is None
    ]
    if deployments is None or replicasets is None:
        return block(failed=failed)
    revisions, incomplete = _revisions(deployments, replicasets)
    start = now - HISTORY_WINDOW
    rows = []
    missing = ["replicaset_history"] if incomplete else []
    verified = {
        record.proposal_id: record
        for record in rollbacks or []
        if start <= record.started_at <= now
    }
    for record in verified.values():
        revision = _retained_revision(record, revisions)
        if revision is None:
            continue
        row = {
            "namespace": namespace,
            "service": record.service,
            "version": f"r{record.target_revision}",
            "occurred_at": iso(record.started_at),
            "kind": "vigil_rollback",
        }
        if record.approved_by:
            row["approved_by"] = record.approved_by
        else:
            missing.append("rollback_approver")
        rows.append(row)
    for revision in revisions:
        if not start <= revision.created_at <= now:
            continue
        if revision.cause.startswith(VIGIL_ROLLBACK_CAUSE) and len(revision.revisions) == 1:
            record = verified.get(revision.cause.removeprefix(VIGIL_ROLLBACK_CAUSE))
            if record is None or record.deployment_uid != revision.deployment_uid:
                missing.append("rollback_classification")
                continue
            if revision.image == record.target_image:
                if _created_during(revision, record):
                    continue
                if record.finished_at is None or revision.created_at < record.started_at:
                    missing.append("replicaset_history")
                    continue
            # 后续发布可继承旧 change-cause。目标镜像和核实时间窗用于排除旧回滚。
        rows.append(
            {
                "namespace": namespace,
                "service": revision.service,
                "version": f"r{min(revision.revisions)}",
                "occurred_at": iso(revision.created_at),
                "kind": "release",
            }
        )
    rows.sort(
        key=lambda row: (row["occurred_at"], row["service"], row["kind"], row["version"]),
        reverse=True,
    )
    return block({"items": rows}, failed=failed, missing=sorted(set(missing)))
