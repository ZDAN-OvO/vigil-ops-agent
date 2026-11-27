"""用真实 PostgreSQL 验证人工恢复后何时可以重新获得自动处置名额。"""

import asyncio
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe
from uuid import uuid4

import httpx
import pytest
from test_remediation_integration import CONFIG, Cluster, Metrics
from test_remediation_store import _grant, _ledgers, _locks, _risk, _rows

from aiops.actions.policy import load_policy
from aiops.actions.remediation_runtime import RemediationRuntime
from aiops.config import Settings
from aiops.contracts import AttemptOutcome, ProposalStatus
from aiops.webhook import build_app


class RecoveryMetrics(Metrics):
    def __init__(self, recovery_end, scenario):
        self.recovery_end = recovery_end
        self.scenario = scenario
        self.queried = asyncio.Event()
        self.resume = asyncio.Event()
        self.recovery_queries = 0

    async def raw_samples(self, selector, start, end):
        points = await super().raw_samples(selector, start, end)
        if end != self.recovery_end:
            return points
        self.queried.set()
        self.recovery_queries += 1
        if self.scenario == "concurrent_new_firing" and self.recovery_queries == 2:
            self.resume.set()
        if self.scenario in {
            "concurrent_older_firing",
            "concurrent_older_recovery",
            "concurrent_new_firing",
            "expired_evaluation_lease",
        }:
            await self.resume.wait()
        if self.scenario == "insufficient_samples":
            return points[-1:]
        if self.scenario == "failed_sample":
            index = len(points) // 2
            return [*points[:index], (points[index][0], 0), *points[index + 1 :]]
        if self.scenario == "failure_before_new_firing":
            return [*points[:-4], *((timestamp, 0) for timestamp, _ in points[-4:])]
        return points


@pytest.mark.parametrize(
    ("scenario", "new_quota"),
    [
        ("failed", True),
        ("compensated", True),
        ("same_group", True),
        ("failure_before_new_firing", True),
        ("unconfirmed_send", False),
        ("insufficient_samples", False),
        ("failed_sample", False),
        ("old_resolved_and_closed_session", False),
        ("too_early", False),
        ("concurrent_older_firing", False),
        ("concurrent_older_recovery", False),
        ("concurrent_new_firing", True),
        ("expired_evaluation_lease", False),
    ],
)
def test_recovery_between_distinct_faults(isolated_serve_dsn, scenario, new_quota):
    async def run():
        async with _ledgers(isolated_serve_dsn) as (ledger, other):
            settings = Settings(
                _env_file=None,
                cluster="vigil-lab",
                webhook_host="127.0.0.2",
                webhook_token=token_urlsafe(32),
                remediation_auto_enabled=True,
            )
            policy = load_policy(CONFIG)
            cluster = Cluster({"replicas": 1})
            next_start = datetime.now(UTC) - timedelta(minutes=1)
            metrics = RecoveryMetrics(next_start, scenario)
            runtime = RemediationRuntime(settings, ledger, policy, cluster, metrics)
            second_runtime = RemediationRuntime(settings, other, policy, cluster, metrics)
            app = build_app(
                settings, sessions=ledger.sessions, safety=ledger.safety, remediation_available=True
            )
            first_start = next_start - timedelta(minutes=40)
            alert = {
                "status": "firing",
                "fingerprint": "aabbccddeeff0011",
                "startsAt": first_start.isoformat(),
                "labels": {
                    "alertname": "VigilReplicaCapacityDegraded",
                    "namespace": "vigil-experiment",
                    "target": "vigil-replica-fixture",
                    "workload_kind": "Deployment",
                    "source": "capacity",
                },
            }
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.2:8091"
            ) as web:

                async def notify(group, *, status="firing", starts=first_start):
                    response = await web.post(
                        "/webhook/alertmanager",
                        headers={
                            "Authorization": f"Bearer {settings.webhook_token.get_secret_value()}"
                        },
                        json={
                            "groupKey": group,
                            "status": status,
                            "alerts": [{**alert, "status": status, "startsAt": starts.isoformat()}],
                        },
                    )
                    assert response.status_code == 200
                    return response.json()

                original_session = (await notify("original"))["session_id"]
                await runtime.evaluate_one()
                [original] = await _rows(ledger, "SELECT * FROM remediation_jobs")
                await runtime.drive(original["proposal_id"])
                claim = await ledger.claim(original["proposal_id"])
                assert claim.job["sent_at"] is not None
                compensated = scenario == "compensated"
                unknown = scenario == "unconfirmed_send"
                compensation = None
                if compensated:
                    child_id = f"act-{uuid4().hex}"
                    child = claim.proposal.model_copy(
                        update={
                            "id": child_id,
                            "idempotency_key": child_id,
                            "parameters": {"replicas": 1},
                            "status": ProposalStatus.APPROVED,
                        }
                    )
                    compensation = child, _risk(child), {}
                await ledger.finish(
                    claim,
                    outcome=(
                        AttemptOutcome.UNKNOWN
                        if unknown
                        else AttemptOutcome.WORSENED
                        if compensated
                        else AttemptOutcome.NO_IMPROVEMENT
                    ),
                    reason="原故障仍需人工排查",
                    result={"written": None if unknown else True},
                    verification={},
                    keep_locked=unknown,
                    compensation=compensation,
                )
                if compensation is not None:
                    child, _, _ = compensation
                    child_claim = await ledger.claim(child.id)
                    await _grant(ledger, child_claim)
                    await ledger.applied(child_claim, {"receipt": {"resource_version": "3"}})
                    await ledger.finish(
                        await ledger.claim(child.id),
                        outcome=AttemptOutcome.WORSENED,
                        reason="已补偿但原故障尚未解决",
                        result={"written": True, "compensated": True},
                        verification={},
                    )
                if scenario == "old_resolved_and_closed_session":
                    await notify(
                        "original", status="resolved", starts=first_start - timedelta(days=1)
                    )
                    async with ledger.sessions.alert_transaction("original") as conn:
                        await ledger.safety.close_session(conn, original_session, reason="人工关闭")
                else:
                    await notify("original", status="resolved")

                # 模拟实际查询留存的历史探针时，处置、补偿和恢复通知都已发生。
                finished = next_start - timedelta(minutes=15)
                notification = (
                    next_start - timedelta(minutes=1)
                    if scenario == "too_early"
                    else finished + timedelta(minutes=1)
                )
                async with ledger.store.transaction() as conn:
                    await conn.execute(
                        "UPDATE remediation_faults SET opened_at = %s", (first_start,)
                    )
                    await conn.execute(
                        "UPDATE remediation_alerts SET resolved_at = %s "
                        "WHERE resolved_at IS NOT NULL",
                        (notification,),
                    )
                    await conn.execute(
                        """
                        UPDATE remediation_jobs SET
                            sent_at = %s, applied_at = CASE WHEN applied_at IS NOT NULL THEN %s END
                        WHERE sent_at IS NOT NULL
                        """,
                        (finished - timedelta(minutes=2), finished - timedelta(minutes=1)),
                    )
                    await conn.execute(
                        """
                        UPDATE action_attempts SET started_at = %s, send_granted_at = %s,
                            finished_at = %s
                        """,
                        (
                            finished - timedelta(minutes=3),
                            finished - timedelta(minutes=2),
                            finished,
                        ),
                    )

                next_group = "original" if scenario == "same_group" else "next"
                next_session = (await notify(next_group, starts=next_start))["session_id"]
                if scenario == "same_group":
                    assert next_session == original_session
                if scenario in {
                    "concurrent_older_firing",
                    "concurrent_older_recovery",
                    "expired_evaluation_lease",
                }:
                    worker = asyncio.create_task(runtime.evaluate_one())
                    try:
                        await asyncio.wait_for(metrics.queried.wait(), timeout=5)
                        if scenario == "concurrent_older_firing":
                            await notify(
                                "different-group", starts=next_start - timedelta(minutes=2)
                            )
                        elif scenario == "concurrent_older_recovery":
                            starts = first_start - timedelta(minutes=1)
                            await notify("different-group", starts=starts)
                            await notify("different-group", status="resolved", starts=starts)
                        else:
                            async with other.store.transaction() as conn:
                                await conn.execute(
                                    """
                                    UPDATE remediation_alerts SET evaluation_expires_at =
                                        clock_timestamp() - interval '1 second'
                                    WHERE group_key = 'next'
                                    """
                                )
                    finally:
                        metrics.resume.set()
                        await worker
                elif scenario == "concurrent_new_firing":
                    await notify("different-group", starts=next_start)
                    await asyncio.wait_for(
                        asyncio.gather(runtime.evaluate_one(), second_runtime.evaluate_one()),
                        timeout=10,
                    )
                else:
                    await runtime.evaluate_one()

                faults = await _rows(
                    ledger, "SELECT id, cleared_at FROM remediation_faults ORDER BY opened_at"
                )
                forward = await _rows(
                    ledger, "SELECT * FROM remediation_jobs WHERE parent_proposal_id IS NULL"
                )
                if new_quota:
                    assert len(faults) == 2
                    assert faults[0]["cleared_at"] <= next_start
                    if scenario == "failure_before_new_firing":
                        assert faults[0]["cleared_at"] < next_start
                    assert faults[1]["cleared_at"] is None
                    [audit] = await _rows(
                        ledger,
                        "SELECT payload FROM audit_events "
                        "WHERE event = 'remediation.fault_cleared' AND correlation_id = %s",
                        (original["proposal_id"],),
                    )
                    assert audit["payload"]["fault_id"] == original["fault_id"]
                    assert audit["payload"]["window"]["failed_slots"] == 0
                    assert audit["payload"]["window"]["slots_collected"] == 10
                    [fresh] = [job for job in forward if job["fault_id"] == faults[1]["id"]]
                    await second_runtime.drive(fresh["proposal_id"])
                    assert len(cluster.sent) == 2
                    if scenario == "same_group":
                        assert fresh["proposal_id"] != original["proposal_id"]
                    if scenario == "concurrent_new_firing":
                        assert metrics.recovery_queries == 2
                else:
                    assert faults == [{"id": original["fault_id"], "cleared_at": None}]
                    assert len(forward) == 1
                    assert not await _rows(
                        ledger,
                        "SELECT 1 FROM audit_events WHERE event = 'remediation.fault_cleared'",
                    )
                    if unknown:
                        assert [lock["proposal_id"] for lock in await _locks(ledger)] == [
                            original["proposal_id"]
                        ]

    asyncio.run(run())
