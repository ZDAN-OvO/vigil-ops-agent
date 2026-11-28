"""在真实 PostgreSQL 中验证巡检与告警共享发送和恢复事务。"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe

import httpx
import pytest
from langchain_core.messages import AIMessage
from test_inspection_run import Clock
from test_remediation_integration import CONFIG, Cluster, Metrics
from test_remediation_store import _grant, _ledgers, _rows

from aiops.actions.policy import load_policy
from aiops.actions.remediation_faults import lock_fault_uid
from aiops.actions.remediation_kube import PatchPlan, WriteUnknown
from aiops.actions.remediation_runtime import RemediationRuntime
from aiops.actions.rollout_undo import ActionError
from aiops.agent.conversation import ConversationDriver
from aiops.config import Settings
from aiops.inspection.config import load_sop
from aiops.inspection.models import Check, Sample
from aiops.inspection.remediation import InspectionRemediation
from aiops.inspection.runtime import InspectionRound
from aiops.inspection.store import InspectionStore
from aiops.webhook import build_app

TARGET = {"namespace": "vigil-experiment", "kind": "Deployment", "name": "vigil-replica-fixture"}
CHECKS = tuple(
    Check(ordinal=index, check_id=name, target=TARGET, max_age_seconds=60)
    for index, name in enumerate(("workload-readiness", "workload-signal"))
)


class ReviewModel:
    _llm_type = "inspection-fixture"

    def __init__(self):
        self.calls = []
        self.bindings = []
        self.mismatch = False

    def bind_tools(self, bindings):
        self.bindings = [tool.__name__ for tool in bindings]
        return self

    async def ainvoke(self, messages):
        payload = json.loads(messages[1].content)
        self.calls.append(payload)
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "inspection-review",
                    "name": "SubmitInspectionReview",
                    "args": {
                        "explanation": "已核对工作负载和证据。",
                        "receipts": [
                            {
                                "candidate_id": item["candidate_id"],
                                "target_uid": item["target_uid"],
                                "observation_ids": ["wrong"]
                                if self.mismatch
                                else item["observation_ids"],
                                "status": "confirmed",
                                "conclusion": "业务异常且实际副本数为一。",
                            }
                            for item in payload["candidates"]
                        ],
                    },
                }
            ],
        )


class Workload(Cluster):
    def __init__(self):
        super().__init__({"replicas": 1})
        self.suffix = ""

    def target(self, *args):
        base = super().target(*args)
        return base.model_copy(update={"uid": base.uid + self.suffix})

    def prepare_restore(self, plan):
        snapshot = type(plan.before)(
            plan.before.workload,
            plan.before.object,
            plan.before.kind,
            plan.before.parameters,
            plan.after,
            plan.after,
        )
        return PatchPlan(snapshot, plan.before.fields, "[]", "test", restore=True)


class Scenario:
    def __init__(self, ledger, other):
        self.ledger = ledger
        self.other = other
        self.clock = Clock(datetime.now(UTC))
        ledger.inspections = InspectionStore(ledger.store, clock=self.clock)
        self.inspections = ledger.inspections
        self.settings = Settings(
            _env_file=None,
            inspection_enabled=True,
            remediation_auto_enabled=True,
            tool_timeout_seconds=1,
            webhook_host="127.0.0.2",
            webhook_token=token_urlsafe(32),
        )
        policy = load_policy(CONFIG / "isolated-acceptance")
        assert policy.error is None
        services = []
        for service in policy.services:
            if service.name == TARGET["name"]:
                primary = next(rule for rule in service.rules if rule.type == "inspection")
                lesser = primary.model_copy(update={"id": "secondary", "priority": 50})
                services.append(service.model_copy(update={"rules": (*service.rules, lesser)}))
            else:
                services.append(service)
        policy = policy.model_copy(update={"services": services})
        self.kube = Workload()
        self.metrics = Metrics()
        self.runtime = RemediationRuntime(self.settings, ledger, policy, self.kube, self.metrics)
        self.sop = load_sop(self.settings.skills_path)
        self.model = ReviewModel()
        driver = ConversationDriver(
            settings=self.settings, store=ledger.store, toolbox=None, model=self.model
        )
        self.handler = InspectionRemediation(driver, self.runtime)

    async def prepare(self):
        self.start = await self.inspections.prepare(self.settings.cluster, 20)

    async def begin(self, *, healthy=False, age=0, advance=20, timeout=90):
        self.clock.advance(advance)
        run = await self.inspections.claim_due(
            cluster=self.settings.cluster,
            started_at=self.start,
            interval_seconds=20,
            timeout_seconds=timeout,
            owner="scheduler",
            sop_hash=self.sop.skill.content_hash,
            checks=CHECKS,
        )
        assert run is not None

        async def sample(check, run_id, remaining):
            uid = self.kube.target(self.settings.cluster, *TARGET.values()).uid
            if check.check_id == "workload-readiness":
                return Sample("healthy", None, self.clock(), {"ready_replicas": 1, "uid": uid})
            return Sample(
                "healthy" if healthy else "abnormal",
                None,
                self.clock() - timedelta(seconds=age),
                {"value": int(healthy)},
            )

        scope = InspectionRound(run, self.inspections, self.sop, sample)
        observations = [await scope.sample(check, initial=True) for check in CHECKS]
        return scope, tuple(observations)

    async def execute(self, *, dispatch=True, **options):
        scope, observations = await self.begin(**options)

        async def drive(_scope, ids):
            if dispatch:
                for identifier in ids:
                    await self.runtime.drive(identifier)

        wait = self.handler._await_dispatch
        self.handler._await_dispatch = drive
        try:
            await self.handler.on_observations(scope, observations)
        finally:
            self.handler._await_dispatch = wait
        await self.inspections.finish(scope.run)
        return await self.inspections.get_run(scope.run.id)

    @asynccontextmanager
    async def web(self):
        app = build_app(
            self.settings,
            sessions=self.ledger.sessions,
            safety=self.ledger.safety,
            remediation_available=True,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.2"
        ) as web:

            async def notify(group, start, status="firing"):
                response = await web.post(
                    "/webhook/alertmanager",
                    headers={
                        "Authorization": "Bearer " + self.settings.webhook_token.get_secret_value(),
                    },
                    json={
                        "groupKey": group,
                        "status": status,
                        "alerts": [
                            {
                                "status": status,
                                "fingerprint": "aabbccddeeff0011",
                                "startsAt": start.isoformat(),
                                "labels": {
                                    "alertname": "VigilReplicaCapacityDegraded",
                                    "namespace": TARGET["namespace"],
                                    "target": TARGET["name"],
                                    "workload_kind": TARGET["kind"],
                                    "source": "capacity",
                                },
                            }
                        ],
                    },
                )
                assert response.status_code == 200

            yield notify


@pytest.mark.parametrize("first", ["inspection", "alert"])
def test_same_fault_shares_quota_across_rules_rounds_and_alerts(isolated_serve_dsn, first):
    async def run():
        async with _ledgers(isolated_serve_dsn) as (ledger, other):
            state = Scenario(ledger, other)
            await state.prepare()
            async with state.web() as notify:
                at = state.clock()
                if first == "alert":
                    await notify("first", at)
                    await state.runtime.evaluate_one()
                    [job] = await _rows(ledger, "SELECT * FROM remediation_jobs")
                    await state.runtime.drive(job["proposal_id"])
                round_one = await state.execute()
                if first == "inspection":
                    await notify("first", at)
                    await state.runtime.evaluate_one()
                restarted = RemediationRuntime(
                    state.settings, other, state.runtime.policy, state.kube, state.metrics
                )
                other.inspections = ledger.inspections
                state.handler.runtime = state.runtime = restarted
                round_two = await state.execute()
            [fault] = await _rows(ledger, "SELECT * FROM remediation_faults")
            [job] = await _rows(ledger, "SELECT * FROM remediation_jobs")
            [alert] = await _rows(ledger, "SELECT * FROM remediation_alerts")
            assert alert["fault_id"] == job["fault_id"] == fault["id"]
            assert len(state.kube.sent) == 1 and job["sent_at"] is not None
            assert round_one.result["rules"][1]["status"] == "lower_priority"
            assert round_two.result["rules"][-1]["status"] == "fault_already_handled"
            assert len(state.model.calls) == 2
            assert sorted(state.model.bindings) == ["ReadInspectionCheck", "SubmitInspectionReview"]
            if first == "inspection":
                assert round_one.result["actions"][job["proposal_id"]]["status"] == "applied"
                [review] = await _rows(ledger, "SELECT * FROM remediation_inspection_reviews")
                assert review["sop_hash"] == round_one.sop_hash
                assert review["rule_id"] == "inspection-replica-capacity"
                assert await _rows(
                    ledger, "SELECT trigger FROM incidents WHERE trigger = 'scheduled'"
                ) == [{"trigger": "scheduled"}, {"trigger": "scheduled"}]

    asyncio.run(run())


@pytest.mark.parametrize("missing_model", [True, False])
def test_unconfirmed_review_leaves_manual_result(isolated_serve_dsn, missing_model):
    async def run():
        async with _ledgers(isolated_serve_dsn) as (ledger, other):
            state = Scenario(ledger, other)
            await state.prepare()
            state.model.mismatch = True
            if missing_model:
                state.handler.driver.model = None
                state.handler.driver.settings = state.settings.model_copy(
                    update={"model_name": None}
                )
            round_one = await state.execute()
            assert round_one.result["rules"][-1]["status"] in {
                "review_unconfirmed",
                "review_evidence_stale_or_mismatched",
            }
            assert not await _rows(ledger, "SELECT * FROM remediation_jobs")
            assert not state.kube.sent

    asyncio.run(run())


async def observe_after(ledger, state, job, seconds):
    state.clock.advance(seconds)
    async with ledger.store.transaction() as conn:
        await conn.execute("UPDATE remediation_jobs SET run_after = clock_timestamp()")
    await state.runtime.drive(job["proposal_id"])


def test_health_clears_fault_and_allows_only_fresh_recurrence(
    isolated_serve_dsn,
):
    async def run():
        async with _ledgers(isolated_serve_dsn) as (ledger, other):
            state = Scenario(ledger, other)
            await state.prepare()
            await state.execute()
            [job] = await _rows(ledger, "SELECT * FROM remediation_jobs")
            await observe_after(ledger, state, job, 1)
            await observe_after(ledger, state, job, 620)
            [unclosed] = await _rows(ledger, "SELECT cleared_at FROM remediation_faults")
            assert unclosed["cleared_at"] is None
            [proposal] = await _rows(ledger, "SELECT status FROM action_proposals")
            assert proposal["status"] == "succeeded"
            healthy = await state.execute(healthy=True)
            assert healthy.result["faults"][0]["status"] == "cleared"
            assert len(state.model.calls) == 1
            [fault] = await _rows(ledger, "SELECT * FROM remediation_faults")
            assert fault["cleared_at"] is not None
            async with state.web() as notify:
                await notify("late", fault["cleared_at"] - timedelta(seconds=1))
                await state.runtime.evaluate_one()
            late = await state.execute(age=25)
            assert late.result["faults"][0]["status"] == "signal_before_clearance"
            assert len(state.kube.sent) == 1
            fresh = await state.execute()
            assert fresh.result["rules"][-1]["status"] == "proposal_created"
            assert len(state.kube.sent) == 2
            faults = await _rows(ledger, "SELECT * FROM remediation_faults")
            assert len(faults) == 2 and sum(row["cleared_at"] is None for row in faults) == 1

    asyncio.run(run())


def test_old_uid_blocks_new_uid_in_both_entries(isolated_serve_dsn):
    async def run():
        async with _ledgers(isolated_serve_dsn) as (ledger, other):
            state = Scenario(ledger, other)
            await state.prepare()
            await state.execute()
            [original] = await _rows(ledger, "SELECT * FROM remediation_faults")
            state.kube.suffix = "-replacement"
            replacement = await state.execute()
            assert replacement.result["faults"][0]["status"] == "previous_uid_open"
            async with state.web() as notify:
                await notify("replacement", state.clock())
                await state.runtime.evaluate_one()
            assert await _rows(ledger, "SELECT * FROM remediation_faults") == [original]
            assert len(await _rows(ledger, "SELECT * FROM remediation_jobs")) == 1
            assert len(state.kube.sent) == 1

    asyncio.run(run())


def test_send_rechecks_deadline_after_waiting_for_uid_lock(isolated_serve_dsn, monkeypatch):
    async def run():
        async with _ledgers(isolated_serve_dsn) as (ledger, other):
            state = Scenario(ledger, other)
            await state.prepare()
            scope, observations = await state.begin(timeout=30)

            async def enqueue_only(_scope, ids):
                assert len(ids) == 1

            monkeypatch.setattr(state.handler, "_await_dispatch", enqueue_only)
            await state.handler.on_observations(scope, observations)
            [job] = await _rows(ledger, "SELECT * FROM remediation_jobs")
            claim = await ledger.claim(job["proposal_id"])
            started = asyncio.Event()
            check_active = state.inspections.require_active_for_send

            async def enter(*args, **kwargs):
                result = await check_active(*args, **kwargs)
                started.set()
                return result

            monkeypatch.setattr(state.inspections, "require_active_for_send", enter)
            async with other.store.transaction() as conn:
                await lock_fault_uid(conn, claim.proposal.target)
                pending = asyncio.create_task(_grant(ledger, claim))
                async with asyncio.timeout(5):
                    await started.wait()
                state.clock.value = scope.run.deadline
            with pytest.raises(ActionError, match="超过期限") as error:
                await pending
            await ledger.fail_unsent(claim, error.value.code, str(error.value))
            await state.inspections.finish(scope.run)
            [saved] = await _rows(ledger, "SELECT sent_at, state FROM remediation_jobs")
            assert saved == {"sent_at": None, "state": "finished"}
            assert not await _rows(ledger, "SELECT * FROM action_attempts")
            result = await state.inspections.get_run(scope.run.id)
            assert result.status == "timed_out"
            assert result.result["actions"][claim.proposal.id]["status"] == "voided"

    asyncio.run(run())


async def assert_uid_locked(ledger, target):
    [row] = await _rows(
        ledger,
        "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 1)) AS acquired",
        (f"{target.cluster}/{target.namespace}/{target.uid}",),
    )
    assert row["acquired"] is False


@pytest.mark.parametrize("first", ["health", "alert"])
def test_health_and_firing_serialize_under_target_and_uid_locks(
    isolated_serve_dsn, monkeypatch, first
):
    from aiops import webhook
    from aiops.inspection import remediation

    async def run():
        async with _ledgers(isolated_serve_dsn) as (ledger, other):
            state = Scenario(ledger, other)
            await state.prepare()
            await state.execute()
            [job] = await _rows(ledger, "SELECT * FROM remediation_jobs")
            await observe_after(ledger, state, job, 1)
            await observe_after(ledger, state, job, 620)
            reached = asyncio.Event()
            release = asyncio.Event()
            target = state.kube.target(state.settings.cluster, *TARGET.values())
            at = state.clock()
            async with state.web() as notify:
                if first == "health":
                    check = remediation.inspection_can_clear

                    async def pause(*args, **kwargs):
                        reached.set()
                        await release.wait()
                        return await check(*args, **kwargs)

                    monkeypatch.setattr(remediation, "inspection_can_clear", pause)
                    healthy = asyncio.create_task(state.execute(healthy=True))
                    async with asyncio.timeout(8):
                        await reached.wait()
                        try:
                            await assert_uid_locked(other, target)
                            alert_entered = asyncio.Event()
                            take_name = webhook.lock_workload_alerts

                            async def enter(*args):
                                alert_entered.set()
                                return await take_name(*args)

                            monkeypatch.setattr(webhook, "lock_workload_alerts", enter)
                            alert = asyncio.create_task(notify("competing", at))
                            await alert_entered.wait()
                        finally:
                            release.set()
                        healthy, _ = await asyncio.gather(healthy, alert)
                    assert healthy.result["faults"][0]["status"] == "cleared"
                else:
                    store_firing = webhook.record_firing

                    async def pause(*args, **kwargs):
                        reached.set()
                        await release.wait()
                        return await store_firing(*args, **kwargs)

                    monkeypatch.setattr(webhook, "record_firing", pause)
                    alert = asyncio.create_task(notify("competing", at))
                    async with asyncio.timeout(8):
                        await reached.wait()
                        health_entered = asyncio.Event()
                        take_name = remediation.lock_workload_alerts

                        async def enter(*args):
                            health_entered.set()
                            return await take_name(*args)

                        monkeypatch.setattr(remediation, "lock_workload_alerts", enter)
                        healthy = asyncio.create_task(state.execute(healthy=True))
                        try:
                            await health_entered.wait()
                        finally:
                            release.set()
                        healthy, _ = await asyncio.gather(healthy, alert)
                    assert healthy.result["faults"][0]["status"] == "healthy"
                await state.runtime.evaluate_one()

            [fault] = await _rows(ledger, "SELECT cleared_at FROM remediation_faults")
            assert (fault["cleared_at"] is not None) == (first == "health")
            assert len(state.kube.sent) == 1

    asyncio.run(run())


def test_finish_holds_uid_lock_when_healthy_round_arrived_during_observation(
    isolated_serve_dsn,
    monkeypatch,
):
    from aiops.actions import remediation_store

    async def run():
        async with _ledgers(isolated_serve_dsn) as (ledger, other):
            state = Scenario(ledger, other)
            await state.prepare()
            await state.execute()
            [job] = await _rows(ledger, "SELECT * FROM remediation_jobs")
            await observe_after(ledger, state, job, 1)
            healthy = await state.execute(healthy=True, advance=620)
            assert healthy.result["faults"][0]["status"] == "healthy"
            entered = asyncio.Event()
            release = asyncio.Event()
            check = remediation_store.inspection_can_clear

            async def pause(*args, **kwargs):
                entered.set()
                await release.wait()
                return await check(*args, **kwargs)

            monkeypatch.setattr(remediation_store, "inspection_can_clear", pause)
            observation = asyncio.create_task(observe_after(ledger, state, job, 0))
            async with asyncio.timeout(8):
                await entered.wait()
                try:
                    target = state.kube.target(state.settings.cluster, *TARGET.values())
                    await assert_uid_locked(other, target)
                finally:
                    release.set()
                await observation
            [fault] = await _rows(ledger, "SELECT cleared_at FROM remediation_faults")
            assert fault["cleared_at"] is not None

    asyncio.run(run())


def test_sent_job_survives_round_timeout_and_compensates_once(isolated_serve_dsn, monkeypatch):
    async def run():
        async with _ledgers(isolated_serve_dsn) as (ledger, other):
            state = Scenario(ledger, other)
            await state.prepare()
            scope, observations = await state.begin(timeout=30)
            applied = ledger.applied

            async def after_deadline(claim, result):
                if claim.job["parent_proposal_id"] is None:
                    state.clock.value = scope.run.deadline
                    await state.inspections.finish(scope.run)
                await applied(claim, result)

            async def drive(_scope, ids):
                for identifier in ids:
                    await state.runtime.drive(identifier)

            monkeypatch.setattr(ledger, "applied", after_deadline)
            monkeypatch.setattr(state.handler, "_await_dispatch", drive)
            await state.handler.on_observations(scope, observations)
            [job] = await _rows(ledger, "SELECT * FROM remediation_jobs")
            assert job["sent_at"] < scope.run.deadline
            assert (await state.inspections.get_run(scope.run.id)).status == "timed_out"
            assert job["state"] == "applying"

            await observe_after(ledger, state, job, 1)
            normal = state.metrics.raw_samples

            async def failed(*args):
                return [(at, 0) for at, _ in await normal(*args)]

            monkeypatch.setattr(state.metrics, "raw_samples", failed)
            await observe_after(ledger, state, job, 301)
            [compensation] = await _rows(
                ledger, "SELECT * FROM remediation_jobs WHERE parent_proposal_id IS NOT NULL"
            )
            assert compensation["inspection_review_id"] == job["inspection_review_id"]
            await state.runtime.drive(compensation["proposal_id"])
            await observe_after(ledger, state, compensation, 1)
            await state.runtime.drive(compensation["proposal_id"])
            assert state.kube.sent == [{"replicas": 2}, {"replicas": 1}]
            final = await state.inspections.get_run(scope.run.id)
            assert final.status == "timed_out"
            assert final.result["actions"][compensation["proposal_id"]]["status"] == "succeeded"
            [fault] = await _rows(ledger, "SELECT cleared_at FROM remediation_faults")
            assert fault["cleared_at"] is None

    asyncio.run(run())


def test_unknown_write_stays_open_even_on_healthy_round(isolated_serve_dsn, monkeypatch):
    async def run():
        async with _ledgers(isolated_serve_dsn) as (ledger, other):
            state = Scenario(ledger, other)
            await state.prepare()

            def lose_receipt(preflight, *, authorize):
                authorize()
                state.kube.sent.append(preflight.plan.after)
                raise WriteUnknown("no_receipt")

            monkeypatch.setattr(state.kube, "send", lose_receipt)
            first = await state.execute()
            [job] = await _rows(ledger, "SELECT * FROM remediation_jobs")
            assert first.result["actions"][job["proposal_id"]]["status"] == "unknown"
            healthy = await state.execute(healthy=True, advance=900)
            assert healthy.result["faults"][0]["status"] == "healthy"
            [fault] = await _rows(ledger, "SELECT cleared_at FROM remediation_faults")
            assert fault["cleared_at"] is None
            assert await _rows(ledger, "SELECT proposal_id FROM action_locks") == [
                {"proposal_id": job["proposal_id"]},
            ]
            await state.execute()
            assert len(state.kube.sent) == 1

    asyncio.run(run())


def test_updated_sample_requires_a_new_model_receipt(isolated_serve_dsn, monkeypatch):
    async def run():
        async with _ledgers(isolated_serve_dsn) as (ledger, other):
            state = Scenario(ledger, other)
            await state.prepare()
            scope, observations = await state.begin()

            async def replace_then_send(_scope, ids):
                assert len(ids) == 1
                await scope.resample(CHECKS[1])
                await state.runtime.drive(ids[0])

            monkeypatch.setattr(state.handler, "_await_dispatch", replace_then_send)
            await state.handler.on_observations(scope, observations)
            final = await state.inspections.get_run(scope.run.id)
            [job] = await _rows(ledger, "SELECT * FROM remediation_jobs")
            assert job["sent_at"] is None
            assert not state.kube.sent
            assert final.result["actions"][job["proposal_id"]]["code"] == (
                "inspection_evidence_changed"
            )

    asyncio.run(run())
