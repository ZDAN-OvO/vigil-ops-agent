import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from aiops.actions.outcome import SampleSummary
from aiops.actions.policy import load_policy
from aiops.actions.remediation_kube import (
    ObservedPod,
    PatchPlan,
    RemediationKubeError,
    Snapshot,
    Verification,
    WriteReceipt,
)
from aiops.actions.remediation_observation import RemediationMetrics, WindowSamples, worsened
from aiops.actions.remediation_runtime import RemediationRuntime
from aiops.config import PROJECT_ROOT
from aiops.contracts import ActionOrigin, ActionTarget, AttemptOutcome

START = datetime(2026, 11, 27, 6, 0, tzinfo=UTC)
END = START + timedelta(minutes=10)
CONTAINER_ID = "a" * 64
CONFIG_SELECTOR = (
    'vigil_config_fixture_loaded_mode{job="vigil-config-state",source="configmap",mode="healthy"}'
)


@pytest.mark.parametrize(
    ("name", "failure", "expect"),
    [
        ("memory", None, AttemptOutcome.IMPROVED),
        ("memory", "slow_recovery", AttemptOutcome.IMPROVED),
        ("memory", "business_recurs", AttemptOutcome.NO_IMPROVEMENT),
        ("memory", "alert_recurs", AttemptOutcome.NO_IMPROVEMENT),
        ("memory", "oom_before_recovery", AttemptOutcome.NO_IMPROVEMENT),
        ("memory", "settling_timeout", AttemptOutcome.UNKNOWN),
        ("memory", "missing_settling_probe", AttemptOutcome.UNKNOWN),
        ("memory", "missing_early_window", AttemptOutcome.UNKNOWN),
        ("memory", "lost_anchor", AttemptOutcome.UNKNOWN),
        ("memory", "early_worsening", AttemptOutcome.WORSENED),
        ("memory", "worsening_after_timeout", AttemptOutcome.WORSENED),
        ("memory", "request_only", AttemptOutcome.IMPROVED),
        ("memory", "oom", AttemptOutcome.NO_IMPROVEMENT),
        ("memory", "oom_at_window_end", AttemptOutcome.IMPROVED),
        ("memory", "counter_changed", AttemptOutcome.UNKNOWN),
        ("memory", "pod_replaced", AttemptOutcome.UNKNOWN),
        ("memory", "forbidden", AttemptOutcome.UNKNOWN),
        ("memory", "legacy_observation", AttemptOutcome.UNKNOWN),
        ("cpu", None, AttemptOutcome.IMPROVED),
        ("cpu", "throttled", AttemptOutcome.NO_IMPROVEMENT),
        ("cpu", "no_periods", AttemptOutcome.UNKNOWN),
        ("cpu", "counter_missing", AttemptOutcome.UNKNOWN),
        ("cpu", "container_replaced", AttemptOutcome.UNKNOWN),
        ("scale", None, AttemptOutcome.IMPROVED),
        ("scale", "rejected_once", AttemptOutcome.NO_IMPROVEMENT),
        ("scale", "too_few_endpoints", AttemptOutcome.NO_IMPROVEMENT),
        ("scale", "stale_endpoint_after_scale_down", AttemptOutcome.UNKNOWN),
        ("scale", "unavailable_batch_clock", AttemptOutcome.UNKNOWN),
        ("configmap", None, AttemptOutcome.IMPROVED),
        ("configmap", "gate", AttemptOutcome.NO_IMPROVEMENT),
        ("configmap", "load_missing_once", AttemptOutcome.UNKNOWN),
        ("configmap", "load_wrong_once", AttemptOutcome.NO_IMPROVEMENT),
        ("env", None, AttemptOutcome.IMPROVED),
        ("env", "gate", AttemptOutcome.NO_IMPROVEMENT),
    ],
)
def test_resolved_business_does_not_replace_independent_fault_signal(
    name,
    failure,
    expect,
    monkeypatch,
):
    resource = name in {"memory", "cpu"}
    settles_late = failure in {
        "slow_recovery",
        "business_recurs",
        "alert_recurs",
        "oom_before_recovery",
        "missing_early_window",
        "lost_anchor",
        "early_worsening",
    }
    recovery = START + timedelta(seconds=102 if settles_late else 0)
    times_out = failure in {"settling_timeout", "missing_settling_probe", "worsening_after_timeout"}
    worsens = failure in {"early_worsening", "worsening_after_timeout"}

    class Clock(datetime):
        at = START

        @classmethod
        def now(cls, tz=None):
            return cls.at

    class Metrics:
        @staticmethod
        async def fresh(selector, at, *, max_age):
            assert selector.startswith("vigil_config_fixture_loaded_mode") and max_age == 60
            return 1.0, (at - timedelta(seconds=1)).timestamp()

        @staticmethod
        async def raw_samples(selector, start, end):
            ts = start.timestamp()
            samples = [(ts + i, 1.0) for i in range(5, int((end - start).total_seconds()), 15)]
            if 'signal="gate"' in selector:
                return [
                    (at, float(failure != "gate" or i != 5)) for i, (at, _) in enumerate(samples)
                ]
            if selector.startswith("vigil_config_fixture_loaded_mode"):
                if name == "configmap":
                    assert selector == CONFIG_SELECTOR
                if failure == "load_missing_once":
                    return samples[:5] + samples[6:]
                if failure == "load_wrong_once":
                    samples[5] = (samples[5][0], 0.0)
                return samples
            if selector.startswith("probe_success") and resource:
                # 用固定的原始抓取时刻模拟探针，不随查询窗口的对齐方式变化。
                first = START.timestamp()
                first_success = first + 180 if times_out else recovery.timestamp()
                points = sorted(
                    {
                        *(first + offset for offset in range(0, 180, 15)),
                        *(first_success + offset for offset in range(0, 720, 15)),
                    }
                )
                if failure == "missing_settling_probe":
                    points = [at for at in points if at >= first_success]
                if failure == "missing_early_window":
                    points = [at for at in points if at >= first + 60]
                if failure == "lost_anchor" and Clock.at > START + timedelta(minutes=4):
                    points.remove(first_success)
                return [
                    (
                        at,
                        200.0
                        if at >= first_success
                        and not (failure == "business_recurs" and at == first_success + 30)
                        else 503.0,
                    )
                    for at in points
                    if start.timestamp() <= at < end.timestamp()
                ]
            if selector.startswith(("probe_success", "up")):
                return samples
            if selector.startswith("kube_pod_container_status_restarts_total"):
                assert 'uid="pod-uid"' in selector
                assert (start, end) == (recovery, recovery + timedelta(minutes=10))
                return [
                    (at, float(failure == "counter_changed" and i == 5))
                    for i, (at, _) in enumerate(samples)
                ]
            if selector.startswith("container_cpu_cfs"):
                assert CONTAINER_ID in selector and 'job="kubelet-cadvisor"' in selector
                throttled = "throttled" in selector
                if throttled and failure == "counter_missing":
                    return []
                step = 0 if failure == "no_periods" else (7 if failure == "throttled" else 0)
                if not throttled and failure != "no_periods":
                    step = 10
                return [(at, float(i * step)) for i, (at, _) in enumerate(samples)]
            if selector.startswith("vigil_replica_fixture_batch_timestamp_seconds"):
                return (
                    []
                    if failure == "unavailable_batch_clock"
                    else [(at, at - 4) for at, _ in samples]
                )
            if selector.startswith("vigil_replica_fixture_endpoints"):
                if failure == "stale_endpoint_after_scale_down":
                    return [(at, 2.0) for at, _ in samples]
                return [
                    (at, 0.0 if failure == "too_few_endpoints" and i == 5 else 1.0)
                    for i, (at, _) in enumerate(samples)
                ]
            assert selector.startswith("vigil_replica_fixture_rejected")
            return [
                (at, float(failure == "rejected_once" and i == 5))
                for i, (at, _) in enumerate(samples)
            ]

    class Kube:
        @staticmethod
        def verify(plan, receipt):
            return Verification("ready")

        @staticmethod
        def observation_pods(plan, receipt, container):
            assert container == "server"
            assert plan.expected_spec_digest == "effect"
            assert receipt.generation == 2
            uid = "pod-uid"
            instance = CONTAINER_ID
            finished = ()
            if Clock.at > START:
                if failure == "forbidden":
                    raise RemediationKubeError("api_error", 403)
                if failure == "oom":
                    finished = (START + timedelta(minutes=2), END + timedelta(seconds=1))
                if failure == "oom_before_recovery":
                    finished = (START + timedelta(seconds=45),)
                if failure == "oom_at_window_end":
                    finished = (END,)
                if failure == "pod_replaced":
                    uid = "replacement"
                if failure == "container_replaced":
                    instance = "b" * 64
            return (ObservedPod("live-pod", uid, f"containerd://{instance}", 0, finished),)

    policy = load_policy(PROJECT_ROOT / "config/remediation")
    service = {
        "memory": "currencyservice",
        "cpu": "currencyservice",
        "scale": "vigil-replica-fixture",
        "configmap": "vigil-config-fixture",
        "env": "vigil-config-fixture",
    }[name]
    target = ActionTarget(
        cluster="vigil-lab",
        namespace="vigil-shop" if service == "currencyservice" else "vigil-experiment",
        kind="Deployment",
        name=service,
        uid="target-uid",
        resource_version="1",
    )
    config = policy.service_for(target)
    assert config is not None
    rule = config.rules[int(name in {"cpu", "env"})]
    params = rule.parameters.model_dump(exclude_none=True)
    before = {
        "memory": {"limit_memory": "16Mi"},
        "cpu": {"limit_cpu": "5m"},
        "scale": {"replicas": 2},
        "configmap": {"value": "degraded"},
        "env": {"value": "degraded"},
    }[name]
    if name == "scale":
        params = {"replicas": 1}
    if failure == "request_only":
        params = {"container": "server", "request_memory": "32Mi"}
        before = {"request_memory": "16Mi"}
    obj = target.model_copy(update={"kind": "ConfigMap"}) if name == "configmap" else target
    plan = PatchPlan(
        Snapshot(target, obj, rule.kind.value, params, before, before),
        {field: params[field] for field in before},
        "[]",
        "effect",
    )
    receipt = WriteReceipt(obj, None if name == "configmap" else 2)
    automated = name in {"memory", "cpu", "configmap"} and failure != "request_only"
    proposal = SimpleNamespace(
        target=target,
        origin=ActionOrigin.RULES if automated else ActionOrigin.MODEL,
        kind=rule.kind,
        parameters=params,
        observation_conditions={
            "verification_plan": {
                "probe": (rule.probe or config.probe).promql,
                "duration_seconds": 600,
                "sample_seconds": 60,
            }
        },
    )
    evidence = {"policy_version": policy.version}
    if automated:
        evidence["trusted"] = {
            "policy_version": policy.version,
            "rule_id": rule.id,
            "labels": {
                "alertname": rule.alertname,
                "namespace": target.namespace,
                "target": target.name,
                **rule.labels,
            },
            "evidence_ids": ("test-evidence",),
            "observed_at": START - timedelta(seconds=75),
            "values": {},
        }
    job = {
        "evidence": evidence,
        "effective_at": None,
        "applied_at": START - timedelta(seconds=10),
        "parent_proposal_id": None,
        "fault_id": "fault" if failure == "alert_recurs" else None,
    }
    claim = SimpleNamespace(
        job=job,
        proposal=proposal,
        attempt=SimpleNamespace(
            snapshot={
                "plan": plan.to_dict(),
                "baseline": {"slots_collected": 5, "failed_slots": 0 if worsens else 5},
            },
            result={"receipt": receipt.to_dict()},
        ),
    )

    @asynccontextmanager
    async def transaction():
        yield connection

    connection = object()
    fault_is_not_clear = AsyncMock(return_value=True)
    settings = SimpleNamespace(action_rollout_timeout_seconds=180.0)
    ledger = SimpleNamespace(reschedule=AsyncMock(), store=SimpleNamespace(transaction=transaction))
    monkeypatch.setattr("aiops.actions.remediation_runtime.datetime", Clock)
    monkeypatch.setattr("aiops.actions.remediation_runtime.fault_is_not_clear", fault_is_not_clear)

    async def run():
        runtime = RemediationRuntime(settings, ledger, policy, Kube(), Metrics())
        runtime._finish = AsyncMock()
        await runtime._observe(claim)
        captured = ledger.reschedule.await_args.kwargs
        job.update(effective_at=captured["effective_at"], observation=captured["observation"])
        assert job["effective_at"] == START
        if name in {"memory", "cpu"} and failure != "legacy_observation":
            assert job["observation"]["fault_pods"]["pod-uid"]["restarts"] == 0
        Clock.at = START + timedelta(minutes=4)
        await runtime._observe(claim)
        job["observation"] = ledger.reschedule.await_args.kwargs["observation"]
        runtime._finish.assert_not_awaited()
        if failure == "legacy_observation":
            job["observation"].pop("fault_pods")
        if resource and not times_out:
            assert job["observation"]["business_recovered_at"] == recovery.isoformat()
        else:
            assert "business_recovered_at" not in job["observation"]

        if times_out or worsens:
            Clock.at = START + timedelta(minutes=5, seconds=15)
        else:
            Clock.at = recovery + timedelta(minutes=10, seconds=15)
        restarted = RemediationRuntime(settings, ledger, policy, Kube(), Metrics())
        restarted._finish = AsyncMock()
        compensation = object()
        restarted._compensation = AsyncMock(return_value=compensation)
        await restarted._observe(claim)
        assert restarted._finish.await_args.args[1] is expect
        progress = restarted._finish.await_args.kwargs["progress"]
        assert job["effective_at"] == START
        if resource:
            assert progress["worsening_window"]["started_at"] == START.isoformat()
            assert progress.get("fault_pods") == job["observation"].get("fault_pods")
        if worsens:
            restarted._compensation.assert_awaited_once_with(claim, plan)
            assert restarted._finish.await_args.kwargs["compensation"] is compensation
            assert progress["worsening_window"]["slots_collected"] == 5
        else:
            restarted._compensation.assert_not_awaited()
        if times_out:
            assert "business_recovered_at" not in progress
            assert progress["slots_collected"] == 0
        if not (times_out or worsens):
            assert progress["slots_collected"] == 10
            assert progress["failed_slots"] == int(failure == "business_recurs")
        if failure == "alert_recurs":
            fault_is_not_clear.assert_awaited_once_with(
                connection, fault_id="fault", started_at=START
            )
        if failure == "business_recurs":
            assert progress["business_recovered_at"] == recovery.isoformat()
        if failure not in {
            "business_recurs",
            "alert_recurs",
            "settling_timeout",
            "missing_settling_probe",
            "missing_early_window",
            "lost_anchor",
            "early_worsening",
            "worsening_after_timeout",
        }:
            assert progress["fault_signals"]

    asyncio.run(run())


@pytest.mark.parametrize("offset,expected", [(-25, 0), (-241, None), (2, None), (None, None)])
def test_gate_uses_prometheus_sample_time(offset, expected):
    at = datetime(2026, 11, 27, 6, 0, tzinfo=UTC)

    def respond(request):
        query = request.url.params["query"]
        result = [{"value": [at.timestamp(), "0"]}]
        if query.startswith("timestamp("):
            result = (
                [{"value": [at.timestamp(), str(at.timestamp() + offset)]}]
                if offset is not None
                else []
            )
        return httpx.Response(
            200,
            json={"status": "success", "data": {"resultType": "vector", "result": result}},
        )

    async def run():
        async with httpx.AsyncClient(
            base_url="http://prometheus",
            transport=httpx.MockTransport(respond),
        ) as client:
            result = await RemediationMetrics(client).fresh(
                'probe_success{job="test"}',
                at,
                max_age=120,
            )
            assert (result[0] if result is not None else None) == expected

    asyncio.run(run())


def test_raw_probe_slots_preserve_failure_and_exclude_boundaries():
    start = datetime(2026, 11, 27, 6, 0, tzinfo=UTC)
    ts = start.timestamp()
    window = WindowSamples.collect(
        [(ts - 1, 0), (ts, 1), (ts + 15, 0), (ts + 15, 0), (ts + 60, 1), (ts + 120, 0)],
        start,
        seconds=60,
        count=2,
    )
    assert window.summary() == SampleSummary(2, 1)
    assert len(window.slots[0]) == 2


def test_compensation_requires_five_comparable_periods():
    start = datetime(2026, 11, 27, 6, 0, tzinfo=UTC)
    points = [((start + timedelta(minutes=i)).timestamp(), 0) for i in range(5)]
    window = WindowSamples.collect(points, start, seconds=60, count=10)
    assert worsened(SampleSummary(5, 1), window)
    assert not worsened(SampleSummary(5, 3), window)
    assert not worsened(SampleSummary(4, 1), window)
