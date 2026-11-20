"""用现行迁移和抓取协议检查发布信号的白名单与过滤。"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import psycopg
import pytest
from db_env import DSN
from psycopg.types.json import Jsonb

from aiops.db import Store
from aiops.monitoring.exporter import create_app, release_metrics
from aiops.monitoring.releases import PostgresRollbackHistory


@pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL，跳过真实库测试")
def test_rollback_query_from_current_schema(cleanup):
    now = datetime.now(UTC)
    start = now - timedelta(hours=24)
    started_at = now - timedelta(minutes=10)
    cluster = "monitoring-" + uuid4().hex[:10]
    namespace = "monitoring-fixture"

    async def scenario():
        async with Store.open(DSN) as store:
            await store.migrate()
        with psycopg.connect(DSN) as conn:
            for index, (scope, kind, status, attempt, at) in enumerate(
                [
                    (cluster, "rollout_undo", "succeeded", "verified", started_at),
                    (cluster, "rollout_undo", "unknown", "unknown", started_at),
                    (cluster, "restart_pod", "succeeded", "verified", started_at),
                    (
                        cluster,
                        "rollout_undo",
                        "succeeded",
                        "verified",
                        start - timedelta(seconds=1),
                    ),
                    ("other", "rollout_undo", "succeeded", "verified", started_at),
                ]
            ):
                proposal = f"{cluster}-{index}"
                cleanup.append(proposal)
                conn.execute(
                    """
                    INSERT INTO action_proposals (
                        id, origin, kind, cluster, namespace, target_kind, target_name, target_uid,
                        target_resource_version, parameters, idempotency_key, reason, status,
                        risk_disposition, risk_reasons, policy_version, created_at
                    ) VALUES (
                        %s, 'operator', %s, %s, %s, 'Deployment', 'catalog', 'catalog-uid',
                        '1', %s, %s, '样例', %s, 'approval', '{}', '1', %s
                    )
                    """,
                    (
                        proposal,
                        kind,
                        scope,
                        namespace,
                        Jsonb({"to_revision": 1}),
                        proposal,
                        status,
                        at - timedelta(minutes=10),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO action_attempts (
                        id, proposal_id, status, idempotency_key, snapshot, request,
                        started_at, finished_at
                    ) VALUES (%s, %s, %s, %s, %s, '{}', %s, %s)
                    """,
                    (
                        proposal,
                        proposal,
                        attempt,
                        proposal,
                        Jsonb(
                            {
                                "to_revision": 1,
                                "target_image": "shop:v1",
                                "approval_hash": "known",
                                "template": "fixture-credential",
                            }
                        ),
                        at,
                        at + timedelta(minutes=2),
                    ),
                )
                for actor, fingerprint, seconds in (
                    ("批准人", "known", 10),
                    ("错误授权的身份", "different", 5),
                ):
                    conn.execute(
                        """
                        INSERT INTO approvals (id, proposal_id, proposal_hash, actor,
                                               decision, reason, decided_at, expires_at)
                        VALUES (%s, %s, %s, %s, 'approved', '样例', %s, %s)
                        """,
                        (
                            f"{proposal}-{fingerprint}",
                            proposal,
                            fingerprint,
                            actor,
                            at - timedelta(seconds=seconds),
                            at + timedelta(minutes=1),
                        ),
                    )
        query = PostgresRollbackHistory(DSN, cluster=cluster, namespace=namespace)
        return await query(start, now)

    (row,) = asyncio.run(scenario())
    assert row.proposal_id == f"{cluster}-0"
    assert row.started_at == started_at
    assert (row.deployment_uid, row.service, row.target_revision, row.target_image) == (
        "catalog-uid",
        "catalog",
        1,
        "shop:v1",
    )
    assert row.approved_by == "批准人"


def _panel():
    return {
        "status": "ok",
        "failed": [],
        "missing": [],
        "data": {
            "items": [
                {
                    "namespace": "vigil-shop",
                    "service": "catalog",
                    "version": "r2",
                    "occurred_at": "2026-11-21T02:00:00+00:00",
                    "kind": "release",
                },
                {
                    "namespace": "vigil-shop",
                    "service": "catalog",
                    "version": "r1",
                    "occurred_at": "2026-11-21T02:10:00+00:00",
                    "kind": "vigil_rollback",
                    "approved_by": "测试身份",
                    "template": "fixture-credential",
                },
                {
                    "namespace": "vigil-shop",
                    "service": "catalog",
                    "version": "r1",
                    "occurred_at": "2026-11-21T03:10:00+00:00",
                    "kind": "vigil_rollback",
                    "approved_by": "测试身份",
                },
            ]
        },
    }


def test_scrape_exports_changes_and_keeps_identity_out():
    class FakeMonitoring:
        async def release_changes(self):
            return _panel()

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(FakeMonitoring())),
            base_url="http://exporter",
        ) as client:
            response = await client.get("/metrics")
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
            assert (await client.post("/metrics")).status_code == 405
            return response.text

    text = asyncio.run(run())
    assert 'vigil_release_source_available{source="replicasets"} 1' in text
    assert 'vigil_release_source_available{source="rollbacks"} 1' in text
    assert 'service="catalog",version="r2",change_type="release",change_at="' in text
    assert text.count('service="catalog",version="r1",change_type="vigil_rollback"') == 2
    assert "测试身份" not in text and "fixture-credential" not in text


def test_release_metrics_identify_missing_sources_without_creating_events():
    failed = {"status": "error", "data": None, "failed": ["deployments"], "missing": []}
    text = release_metrics(failed)
    assert 'vigil_release_source_available{source="replicasets"} 0' in text
    assert 'vigil_release_source_available{source="rollbacks"} 0' in text
    assert "vigil_release_timestamp_seconds{" not in text
