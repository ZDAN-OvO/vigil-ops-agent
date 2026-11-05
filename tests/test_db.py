"""业务表在真实 PostgreSQL 上的读写；未配置实验库时跳过，不伪造通过。"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest

from aiops.config import Settings
from aiops.contracts import (
    AuditEvent,
    Budget,
    Evidence,
    Hypothesis,
    Incident,
    Investigation,
    InvestigationStatus,
    TimeRange,
    ToolCall,
)
from aiops.db import Store
from aiops.knowledge import Runbook

DSN = Settings().database_url
pytestmark = pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL，跳过真实库测试")

START = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def ids():
    """为每个用例生成独立标识，结束后按外键顺序清理。"""
    tag = uuid4().hex[:12]
    values = {
        "incident": f"inc-{tag}",
        "investigation": f"inv-{tag}",
        "hypothesis": f"hyp-{tag}",
        "tool_call": f"call-{tag}",
        "evidence": f"ev-{tag}",
        "correlation": f"corr-{tag}",
        "audit": f"audit-{tag}",
    }
    yield values
    with psycopg.connect(DSN) as conn:
        conn.execute("DELETE FROM audit_events WHERE correlation_id = %s", (values["correlation"],))
        conn.execute("DELETE FROM evidence WHERE investigation_id = %s", (values["investigation"],))
        conn.execute(
            "DELETE FROM tool_calls WHERE investigation_id = %s", (values["investigation"],)
        )
        conn.execute(
            "DELETE FROM hypotheses WHERE investigation_id = %s", (values["investigation"],)
        )
        conn.execute("DELETE FROM investigations WHERE id = %s", (values["investigation"],))
        conn.execute("DELETE FROM incidents WHERE id = %s", (values["incident"],))


def incident(ids):
    return Incident(
        id=ids["incident"],
        environment="local",
        cluster="vigil-lab",
        namespace="vigil-shop",
        service="frontend",
        trigger="manual",
        symptoms="首页 500",
        time_range=TimeRange(start=START, end=START + timedelta(minutes=10)),
    )


def investigation(ids):
    return Investigation(
        id=ids["investigation"],
        incident_id=ids["incident"],
        status=InvestigationStatus.RUNNING,
        model="test-model",
        policy_version="test-policy",
        budget=Budget(max_tool_calls=5, max_model_calls=5, max_wall_clock_seconds=60),
        started_at=START,
    )


def test_migrations_can_run_repeatedly():
    async def scenario():
        async with Store.open(DSN) as store:
            await store.migrate()
            return await store.migrate()

    assert run(scenario()) == []


def test_investigation_records_round_trip(ids):
    async def scenario():
        async with Store.open(DSN) as store:
            await store.migrate()
            await store.save_incident(incident(ids))
            await store.create_investigation(investigation(ids))
            await store.save_hypothesis(
                Hypothesis(
                    id=ids["hypothesis"],
                    investigation_id=ids["investigation"],
                    statement="坏发布导致首页失败",
                    expected_observations=["probe_success=0"],
                )
            )
            await store.save_tool_call(
                ToolCall(
                    id=ids["tool_call"],
                    investigation_id=ids["investigation"],
                    tool="prometheus.query",
                    parameters={"expression": "probe_success"},
                    time_range=TimeRange(start=START, end=START + timedelta(minutes=5)),
                    started_at=START,
                    finished_at=START + timedelta(seconds=1),
                )
            )
            await store.save_evidence(
                Evidence(
                    id=ids["evidence"],
                    investigation_id=ids["investigation"],
                    hypothesis_id=ids["hypothesis"],
                    tool_call_id=ids["tool_call"],
                    source="prometheus",
                    independence_key="frontend-probe",
                    observation="probe_success 在故障窗口内为 0",
                    relation="supporting",
                    raw_ref="runtime/test/probe.json",
                    time_range=TimeRange(start=START, end=START + timedelta(minutes=5)),
                )
            )
            await store.append_audit(
                [
                    AuditEvent(
                        id=ids["audit"],
                        correlation_id=ids["correlation"],
                        actor="test-runner",
                        event="investigation.started",
                        occurred_at=START,
                        payload={"model": "test-model"},
                    )
                ]
            )
            await store.add_usage(ids["investigation"], tool_calls=1)

            saved_incident = await store.get_incident(ids["incident"])
            saved = await store.get_investigation(ids["investigation"])
            hypotheses = await store.list_hypotheses(ids["investigation"])
            calls = await store.list_tool_calls(ids["investigation"])
            evidence = await store.list_evidence(ids["investigation"])
            audit = await store.list_audit(ids["correlation"])
        return saved_incident, saved, hypotheses, calls, evidence, audit

    saved_incident, saved, hypotheses, calls, evidence, audit = run(scenario())
    assert saved_incident is not None
    assert saved_incident.time_range == incident(ids).time_range
    assert saved is not None
    assert saved.status is InvestigationStatus.RUNNING
    assert saved.usage.tool_calls == 1
    assert [h.statement for h in hypotheses] == ["坏发布导致首页失败"]
    assert calls[0].parameters == {"expression": "probe_success"}
    assert evidence[0].relation == "supporting"
    assert audit[0].payload == {"model": "test-model"}


def test_terminal_status_without_outcome_is_rejected(ids):
    async def scenario():
        async with Store.open(DSN) as store:
            await store.migrate()
            await store.save_incident(incident(ids))
            await store.create_investigation(investigation(ids))
            with pytest.raises(ValueError):
                await store.set_investigation_status(
                    ids["investigation"], InvestigationStatus.COMPLETED
                )

    run(scenario())


def test_completed_requires_conclusion(ids):
    async def scenario():
        async with Store.open(DSN) as store:
            await store.migrate()
            await store.save_incident(incident(ids))
            await store.create_investigation(investigation(ids))
            with pytest.raises(ValueError):
                await store.set_investigation_status(
                    ids["investigation"],
                    InvestigationStatus.COMPLETED,
                    finished_at=START + timedelta(minutes=2),
                )
            await store.set_investigation_status(
                ids["investigation"],
                InvestigationStatus.INCONCLUSIVE,
                termination_reason="证据不足",
                finished_at=START + timedelta(minutes=2),
            )
            saved = await store.get_investigation(ids["investigation"])
        return saved

    saved = run(scenario())
    assert saved is not None
    assert saved.status is InvestigationStatus.INCONCLUSIVE
    assert saved.termination_reason == "证据不足"


def test_runbook_roundtrip_and_query(tmp_path):
    runbook = Runbook(
        id="rb-test-oom",
        version=1,
        title="测试 runbook",
        summary="内存限额排查",
        applies_to=("currencyservice",),
        symptoms=("OOMKilled",),
        steps=("比较历史模板",),
        source="tests",
    )

    async def scenario():
        async with Store.open(DSN) as store:
            await store.migrate()
            await store.upsert_runbook(runbook)
            by_service = await store.list_runbooks(service="currencyservice", limit=5)
            by_other = await store.list_runbooks(service="frontend", limit=5)
            by_query = await store.list_runbooks(query="OOMKilled", limit=5)
            detail = await store.get_runbook("rb-test-oom")
            async with store._pool.connection() as conn:
                await conn.execute("DELETE FROM runbooks WHERE id = %s", ("rb-test-oom",))
            return by_service, by_other, by_query, detail

    by_service, by_other, by_query, detail = run(scenario())

    assert "rb-test-oom" in [item.id for item in by_service]
    assert all(item.id != "rb-test-oom" for item in by_other)
    assert "rb-test-oom" in [item.id for item in by_query]
    assert detail is not None and detail.steps == ("比较历史模板",)
