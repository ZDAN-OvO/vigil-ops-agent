"""告警入口：白名单映射、鉴权与按指纹去重。"""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from starlette.testclient import TestClient

from aiops.config import Settings
from aiops.db import Store
from aiops.webhook import build_app, map_firing_alerts

DSN = Settings().database_url
pytestmark = pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL，跳过真实库测试")

TOKEN = "test-token"


def payload(fingerprint: str) -> dict:
    return {
        "version": "4",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "FrontendProbeFailed",
                    "severity": "critical",
                    "scenario": "f01-bad-release",
                    "job": "frontend-probe",
                    "namespace": "vigil-shop",
                },
                "annotations": {"summary": "前端首页黑盒探测失败"},
                "startsAt": "2026-11-04T10:00:00Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "fingerprint": fingerprint,
            }
        ],
    }


def settings() -> Settings:
    return Settings(
        _env_file=None,
        database_url=DSN,
        webhook_token=TOKEN,
        model_name="scripted",
        dedup_minutes=10,
    )


def test_scenario_label_never_reaches_the_incident():
    events = map_firing_alerts(payload("fp-1"))

    assert len(events) == 1
    assert events[0].fingerprint == "fp-1"
    assert events[0].service == "frontend-probe"
    assert "scenario" not in events[0].labels
    assert "f01-bad-release" not in str(events[0])


def test_resolved_alerts_are_ignored():
    body = payload("fp-2")
    body["alerts"][0]["status"] = "resolved"

    assert map_firing_alerts(body) == []


@pytest.fixture
def fingerprint():
    return f"fp-{uuid4().hex[:10]}"


@pytest.fixture
def cleanup(fingerprint):
    yield
    with psycopg.connect(DSN) as conn:
        incidents = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM incidents WHERE %s = ANY(alert_ids)", (fingerprint,)
            ).fetchall()
        ]
        for incident_id in incidents:
            investigations = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM investigations WHERE incident_id = %s", (incident_id,)
                ).fetchall()
            ]
            for investigation_id in investigations:
                conn.execute(
                    "DELETE FROM audit_events WHERE correlation_id = %s", (investigation_id,)
                )
                conn.execute(
                    "DELETE FROM evidence WHERE investigation_id = %s", (investigation_id,)
                )
                conn.execute(
                    "DELETE FROM tool_calls WHERE investigation_id = %s", (investigation_id,)
                )
                conn.execute(
                    "DELETE FROM hypotheses WHERE investigation_id = %s", (investigation_id,)
                )
                conn.execute("DELETE FROM checkpoints WHERE thread_id = %s", (investigation_id,))
                conn.execute(
                    "DELETE FROM checkpoint_writes WHERE thread_id = %s", (investigation_id,)
                )
                conn.execute("DELETE FROM investigations WHERE id = %s", (investigation_id,))
            conn.execute("DELETE FROM incidents WHERE id = %s", (incident_id,))


def test_webhook_accepts_firing_alert_and_deduplicates(fingerprint, cleanup):
    calls = []

    async def runner(store, settings, incident, investigation):
        calls.append(investigation.id)

    app = build_app(settings(), runner=runner)
    with TestClient(app) as client:
        first = client.post(
            "/webhook/alertmanager",
            json=payload(fingerprint),
            headers={"x-vigil-token": TOKEN},
        )
        assert first.status_code == 202
        body = first.json()
        assert body["status"] == "accepted"

        for _ in range(100):
            if calls:
                break
            time.sleep(0.02)
        assert calls == [body["investigation_id"]]

        second = client.post(
            "/webhook/alertmanager",
            json=payload(fingerprint),
            headers={"x-vigil-token": TOKEN},
        )
        assert second.status_code == 200
        assert second.json() == {"status": "deduplicated", "incident_id": body["incident_id"]}

    with psycopg.connect(DSN) as conn:
        count = conn.execute(
            "SELECT count(*) FROM incidents WHERE %s = ANY(alert_ids)", (fingerprint,)
        ).fetchone()[0]
    assert count == 1


def test_webhook_rejects_missing_token(fingerprint, cleanup):
    app = build_app(settings(), runner=None)
    with TestClient(app) as client:
        response = client.post("/webhook/alertmanager", json=payload(fingerprint))
    assert response.status_code == 401


def test_notification_without_firing_alerts_is_ignored(fingerprint):
    app = build_app(settings(), runner=None)
    body = payload(fingerprint)
    body["alerts"] = []
    with TestClient(app) as client:
        response = client.post("/webhook/alertmanager", json=body, headers={"x-vigil-token": TOKEN})
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


def test_store_finds_recent_incident_by_fingerprint(fingerprint, cleanup):
    from aiops.webhook import build_incident

    async def scenario():
        async with Store.open(DSN) as store:
            await store.migrate()
            now = datetime.now(UTC)
            events = map_firing_alerts(payload(fingerprint))
            incident = build_incident(events, settings(), now=now)
            await store.save_incident(incident)
            found = await store.find_recent_incident(
                [fingerprint], since=now - timedelta(minutes=5)
            )
            missing = await store.find_recent_incident(["other"], since=now)
        return found, missing

    found, missing = asyncio.run(scenario())
    assert found is not None and fingerprint in found.alert_ids
    assert missing is None
