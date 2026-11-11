"""业务表在真实 PostgreSQL 上的读写；未配置实验库时跳过，不伪造通过。"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest

from aiops.config import Settings
from aiops.contracts import (
    ActionAttempt,
    ActionKind,
    ActionOrigin,
    ActionProposal,
    ActionTarget,
    AttemptStatus,
    AuditEvent,
    Budget,
    Evidence,
    Hypothesis,
    HypothesisEvidence,
    Incident,
    Investigation,
    InvestigationStatus,
    InvestigationTask,
    Observation,
    ObservationStatus,
    ProposalStatus,
    RiskDecision,
    RiskDisposition,
    TaskPhase,
    TimeRange,
    ToolCall,
    ToolRequest,
    ToolRequestState,
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
        conn.execute(
            "DELETE FROM tool_requests WHERE investigation_id = %s", (values["investigation"],)
        )
        conn.execute(
            "DELETE FROM hypothesis_evidence WHERE investigation_id = %s",
            (values["investigation"],),
        )
        conn.execute(
            "DELETE FROM observations WHERE investigation_id = %s", (values["investigation"],)
        )
        conn.execute(
            """
            DELETE FROM hypothesis_revisions
             WHERE hypothesis_id IN (
                SELECT id FROM hypotheses WHERE investigation_id = %s
             )
            """,
            (values["investigation"],),
        )
        for proposal_id in [
            row[0]
            for row in conn.execute(
                "SELECT id FROM action_proposals WHERE investigation_id = %s",
                (values["investigation"],),
            ).fetchall()
        ]:
            conn.execute("DELETE FROM action_followups WHERE proposal_id = %s", (proposal_id,))
            conn.execute("DELETE FROM investigation_tasks WHERE proposal_id = %s", (proposal_id,))
            conn.execute("DELETE FROM action_attempts WHERE proposal_id = %s", (proposal_id,))
            conn.execute("DELETE FROM approvals WHERE proposal_id = %s", (proposal_id,))
            conn.execute("DELETE FROM action_locks WHERE proposal_id = %s", (proposal_id,))
            conn.execute("DELETE FROM action_proposals WHERE id = %s", (proposal_id,))
        conn.execute(
            "DELETE FROM investigation_tasks WHERE investigation_id = %s",
            (values["investigation"],),
        )
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


def _task(investigation_id: str) -> InvestigationTask:
    return InvestigationTask(
        investigation_id=investigation_id,
        phase=TaskPhase.RUNNING,
        runtime_version="langchain-agent-1",
        calendar_started_at=START,
    )


def test_auto_stop_gate_spans_action_kinds_but_not_workloads(ids):
    """停止门按工作负载而不是动作类型生效；另一个工作负载不受影响。"""

    async def run():
        async with Store.open(DSN) as store:
            await store.migrate()
            await store.save_incident(incident(ids))
            await store.create_investigation(investigation(ids))
            workload = f"uid-workload-{ids['investigation'][-6:]}"
            restart = ActionProposal(
                id=f"act-{uuid4().hex[:12]}",
                kind=ActionKind.RESTART_POD,
                target=ActionTarget(
                    cluster="vigil-lab",
                    namespace="vigil-shop",
                    kind="Pod",
                    name="pod-a",
                    uid="pod-uid-a",
                    resource_version="1",
                ),
                parameters={"workload": workload, "replica_set": "rs-a"},
                idempotency_key=f"restart:{uuid4().hex}",
                reason="测试",
                created_at=START,
                origin=ActionOrigin.OPERATOR,
                investigation_id=ids["investigation"],
                status=ProposalStatus.APPROVED,
            )
            await store.save_proposal(
                restart,
                RiskDecision(
                    proposal_id=restart.id,
                    disposition=RiskDisposition.AUTO,
                    policy_version="test",
                    reasons=["测试"],
                    requires_compensation=False,
                ),
            )
            await store.save_attempt(
                ActionAttempt(
                    id=f"att-{uuid4().hex[:12]}",
                    proposal_id=restart.id,
                    status=AttemptStatus.FAILED,
                    idempotency_key=restart.idempotency_key,
                    snapshot={},
                    request={},
                    started_at=START,
                    finished_at=START,
                )
            )
            rollout = ActionProposal(
                id=f"act-{uuid4().hex[:12]}",
                kind=ActionKind.ROLLOUT_UNDO,
                target=ActionTarget(
                    cluster="vigil-lab",
                    namespace="vigil-shop",
                    kind="Deployment",
                    name="frontend",
                    uid=f"uid-deploy-{ids['investigation'][-6:]}",
                    resource_version="2",
                ),
                parameters={"workload": workload},
                idempotency_key=f"rollout:{uuid4().hex}",
                reason="测试",
                created_at=START + timedelta(seconds=1),
                origin=ActionOrigin.OPERATOR,
                investigation_id=ids["investigation"],
                status=ProposalStatus.APPROVED,
            )
            await store.save_proposal(
                rollout,
                RiskDecision(
                    proposal_id=rollout.id,
                    disposition=RiskDisposition.AUTO,
                    policy_version="test",
                    reasons=["测试"],
                    requires_compensation=False,
                ),
            )
            same = await store.latest_proposal_blocks_auto(
                "vigil-lab", "vigil-shop", workload, "rs-a"
            )
            other = await store.latest_proposal_blocks_auto(
                "vigil-lab", "vigil-shop", "uid-other-workload", "rs-b"
            )
            return same, other

    same, other = asyncio.run(run())
    assert same is True
    assert other is False


def test_task_lease_and_usage_round_trip(ids):
    async def scenario():
        async with Store.open(DSN) as store:
            await store.migrate()
            await store.save_incident(incident(ids))
            await store.create_investigation(investigation(ids))
            created = await store.create_task(_task(ids["investigation"]))
            duplicate = await store.create_task(_task(ids["investigation"]))
            first_lease = await store.acquire_task_lease(
                ids["investigation"], "owner-a", seconds=60
            )
            second_lease = await store.acquire_task_lease(
                ids["investigation"], "owner-b", seconds=60
            )
            same_owner = await store.acquire_task_lease(ids["investigation"], "owner-a", seconds=60)
            await store.release_task_lease(ids["investigation"], "owner-a")
            second_after_release = await store.acquire_task_lease(
                ids["investigation"], "owner-b", seconds=60
            )
            await store.update_task_usage(
                ids["investigation"],
                used_model_calls=3,
                used_tool_calls=5,
                investigation_seconds=12.5,
                action_seconds=4.25,
                no_progress_rounds=1,
            )
            saved = await store.get_task(ids["investigation"])
        return (
            created,
            duplicate,
            first_lease,
            second_lease,
            same_owner,
            second_after_release,
            saved,
        )

    (created, duplicate, first_lease, second_lease, same_owner, second_after_release, saved) = run(
        scenario()
    )
    assert created is True
    assert duplicate is False
    assert first_lease is True
    assert second_lease is False
    assert same_owner is True
    assert second_after_release is True
    assert saved.used_model_calls == 3
    assert saved.used_tool_calls == 5
    assert saved.investigation_seconds == 12.5
    assert saved.action_seconds == 4.25
    assert saved.no_progress_rounds == 1


def test_task_phase_constraints_hold_in_database(ids):
    async def scenario():
        async with Store.open(DSN) as store:
            await store.migrate()
            await store.save_incident(incident(ids))
            await store.create_investigation(investigation(ids))
            await store.create_task(_task(ids["investigation"]))
            errors = []
            for statement in (
                "UPDATE investigation_tasks SET phase = 'terminal' WHERE investigation_id = %s",
                "UPDATE investigation_tasks SET phase = 'waiting_approval' "
                "WHERE investigation_id = %s",
                "UPDATE investigation_tasks SET outcome = 'recovered' WHERE investigation_id = %s",
            ):
                # 每条语句用独立连接：失败的语句会中止当前事务。
                async with store._pool.connection() as conn:
                    try:
                        await conn.execute(statement, (ids["investigation"],))
                        errors.append(None)
                    except psycopg.errors.CheckViolation:
                        errors.append("check")
        return errors

    assert run(scenario()) == ["check", "check", "check"]


def test_observation_relation_and_hypothesis_revisions(ids):
    async def scenario():
        async with Store.open(DSN) as store:
            await store.migrate()
            await store.save_incident(incident(ids))
            await store.create_investigation(investigation(ids))
            await store.save_hypothesis(
                Hypothesis(
                    id=ids["hypothesis"],
                    investigation_id=ids["investigation"],
                    statement="初始假设",
                    expected_observations=["a"],
                    refutation_criteria=["b"],
                )
            )
            await store.save_tool_call(
                ToolCall(
                    id=ids["tool_call"],
                    investigation_id=ids["investigation"],
                    tool="kubernetes.list_pods",
                    parameters={"selector": "app=x"},
                    time_range=TimeRange(start=START, end=START + timedelta(minutes=5)),
                    started_at=START,
                    finished_at=START + timedelta(seconds=1),
                )
            )
            await store.save_observation(
                Observation(
                    id=ids["evidence"],
                    investigation_id=ids["investigation"],
                    tool_call_id=ids["tool_call"],
                    source="kubernetes",
                    independence_key="kubernetes:pods",
                    summary="Pod 就绪但 /work 500",
                    raw_ref="runtime/test/pods.json",
                    status=ObservationStatus.OK,
                    time_range=TimeRange(start=START, end=START + timedelta(minutes=5)),
                )
            )
            await store.upsert_hypothesis_evidence(
                HypothesisEvidence(
                    id=f"he-{ids['evidence']}",
                    investigation_id=ids["investigation"],
                    hypothesis_id=ids["hypothesis"],
                    observation_id=ids["evidence"],
                    relation="neutral",
                    rationale="先标记中性",
                )
            )
            await store.upsert_hypothesis_evidence(
                HypothesisEvidence(
                    id=f"he2-{ids['evidence']}",
                    investigation_id=ids["investigation"],
                    hypothesis_id=ids["hypothesis"],
                    observation_id=ids["evidence"],
                    relation="supporting",
                    rationale="改成支持",
                )
            )
            updated = await store.update_hypothesis(
                ids["hypothesis"],
                statement="细化后的假设",
                expected_observations=["c"],
                refutation_criteria=["d"],
                status="supported",
                update_reason="新证据支持",
                actor="test",
            )
            relations = await store.list_hypothesis_evidence(ids["investigation"])
            observations = await store.list_observations(ids["investigation"])
            revisions = await store.list_hypothesis_revisions(ids["investigation"])
        return updated, relations, observations, revisions

    updated, relations, observations, revisions = run(scenario())
    assert updated.revision == 2
    assert updated.status.value == "supported"
    assert len(relations) == 1 and relations[0].relation == "supporting"
    assert [item.summary for item in observations] == ["Pod 就绪但 /work 500"]
    assert [row["revision"] for row in revisions] == [1, 2]
    assert revisions[-1]["update_reason"] == "新证据支持"


def test_one_observation_can_support_multiple_hypotheses(ids):
    async def scenario():
        async with Store.open(DSN) as store:
            await store.migrate()
            await store.save_incident(incident(ids))
            await store.create_investigation(investigation(ids))
            first = f"{ids['hypothesis']}-a"
            second = f"{ids['hypothesis']}-b"
            for hid, statement in ((first, "假设 A"), (second, "假设 B")):
                await store.save_hypothesis(
                    Hypothesis(
                        id=hid,
                        investigation_id=ids["investigation"],
                        statement=statement,
                        expected_observations=["a"],
                        refutation_criteria=["b"],
                    )
                )
            await store.save_tool_call(
                ToolCall(
                    id=ids["tool_call"],
                    investigation_id=ids["investigation"],
                    tool="kubernetes.list_pods",
                    parameters={"selector": "app=x"},
                    time_range=TimeRange(start=START, end=START + timedelta(minutes=5)),
                    started_at=START,
                    finished_at=START + timedelta(seconds=1),
                )
            )
            await store.save_observation(
                Observation(
                    id=ids["evidence"],
                    investigation_id=ids["investigation"],
                    tool_call_id=ids["tool_call"],
                    source="kubernetes",
                    independence_key="kubernetes:pods",
                    summary="同一观测可以支持多个竞争假设",
                    raw_ref="runtime/test/pods.json",
                    status=ObservationStatus.OK,
                    time_range=TimeRange(start=START, end=START + timedelta(minutes=5)),
                )
            )
            for index, hid in enumerate((first, second)):
                await store.upsert_hypothesis_evidence(
                    HypothesisEvidence(
                        id=f"he-{index}-{ids['evidence']}",
                        investigation_id=ids["investigation"],
                        hypothesis_id=hid,
                        observation_id=ids["evidence"],
                        relation="supporting",
                        rationale=f"支持 {hid}",
                    )
                )
            return await store.list_hypothesis_evidence(ids["investigation"])

    relations = run(scenario())
    assert len(relations) == 2
    assert {item.observation_id for item in relations} == {ids["evidence"]}
    assert {item.hypothesis_id for item in relations} == {
        f"{ids['hypothesis']}-a",
        f"{ids['hypothesis']}-b",
    }


def test_tool_request_ledger_is_unique_per_call(ids):
    async def scenario():
        async with Store.open(DSN) as store:
            await store.migrate()
            await store.save_incident(incident(ids))
            await store.create_investigation(investigation(ids))
            request = ToolRequest(
                investigation_id=ids["investigation"],
                tool_call_id=ids["tool_call"],
                tool="kubernetes.list_pods",
                arguments={"selector": "app=x"},
                state=ToolRequestState.RUNNING,
                started_at=START,
            )
            first = await store.start_tool_request(request)
            second = await store.start_tool_request(request)
            await store.finish_tool_request(
                ids["investigation"],
                ids["tool_call"],
                state=ToolRequestState.OBSERVED,
                result={"tool_message": "ok"},
            )
            saved = await store.get_tool_request(ids["investigation"], ids["tool_call"])
            listed = await store.list_tool_requests(ids["investigation"])
        return first, second, saved, listed

    first, second, saved, listed = run(scenario())
    assert first is True
    assert second is False
    assert saved.state is ToolRequestState.OBSERVED
    assert saved.result == {"tool_message": "ok"}
    assert len(listed) == 1


def test_investigation_without_task_is_readable(ids):
    async def scenario():
        async with Store.open(DSN) as store:
            await store.migrate()
            await store.save_incident(incident(ids))
            await store.create_investigation(investigation(ids))
            task = await store.get_task(ids["investigation"])
            saved = await store.get_investigation(ids["investigation"])
            listed = await store.list_investigations(ids["incident"])
        return task, saved, listed

    task, saved, listed = run(scenario())
    assert task is None
    assert saved.budget.max_action_seconds == 450
    assert [item.id for item in listed] == [saved.id]
