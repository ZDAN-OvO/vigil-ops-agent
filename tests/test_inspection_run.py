"""定时巡检的只读采集、轮次归属与会话历史。"""

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from aiops.config import Settings
from aiops.db import Store
from aiops.inspection.collector import InspectionCollector, plan_checks
from aiops.inspection.config import InspectionConfigError, Manifest, load_manifest, load_sop
from aiops.inspection.models import Sample
from aiops.inspection.runtime import InspectionScheduler
from aiops.inspection.store import InspectionStore
from aiops.investigation.factory import open_inspection
from aiops.sessions.models import LEASE_SECONDS, LeaseLost, SessionClosed
from aiops.sessions.store import SessionStore
from aiops.tools.kubernetes import KubernetesRead
from aiops.tools.prometheus import PrometheusClient

NOW = datetime(2026, 11, 20, tzinfo=UTC)
SOP_FILE = Path("proactive-inspection") / "SKILL.md"
MANIFEST = """
version: 1
cluster: vigil-lab
workloads:
  - namespace: shop
    kind: Deployment
    name: checkout
    readiness_max_age_seconds: 60
    signal:
      metric: checkout_success_ratio
      labels:
        service: checkout
      minimum: 0.99
      max_age_seconds: 60
nodes:
  - name: worker-1
    max_age_seconds: 60
entries:
  - probe_id: https://checkout.example
    max_age_seconds: 60
""".replace("      min_unused: false\n", "")


@dataclass
class Clock:
    value: datetime = NOW

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def settings(tmp_path):
    config = tmp_path / "inspection.yaml"
    config.write_text(MANIFEST, encoding="utf-8")
    return Settings(
        _env_file=None,
        inspection_config=config,
        runtime_dir=tmp_path / "runtime",
    )


def test_disabled_and_invalid_inspection_keep_other_service_dependencies_open(settings, caplog):
    async def scenario():
        assert not settings.inspection_enabled
        assert settings.inspection_interval_seconds == 900
        assert settings.inspection_timeout_seconds == 600
        # Disabled 分支在没有数据库对象时也不装配巡检依赖。
        async with open_inspection(settings, store=None) as scheduler:
            assert scheduler is None
        invalid = settings.model_copy(
            update={"inspection_enabled": True, "inspection_config": None}
        )
        async with open_inspection(invalid, store=None) as scheduler:
            assert scheduler is None
        assert "缺少 VIGIL_INSPECTION_CONFIG" in caplog.text
        invalid = settings.model_copy(
            update={"inspection_enabled": True, "inspection_timeout_seconds": "illegal"}
        )
        async with open_inspection(invalid, store=None) as scheduler:
            assert scheduler is None
        assert "VIGIL_INSPECTION_TIMEOUT_SECONDS" in caplog.text

    asyncio.run(scenario())


def test_configuration_limits_scope_and_binds_validated_skill(settings, tmp_path):
    manifest = load_manifest(settings)
    assert isinstance(manifest, Manifest)
    original = load_sop(settings.skills_path)
    folder = tmp_path / "skills"
    path = folder / SOP_FILE
    path.parent.mkdir(parents=True)
    body = original.skill.content.replace(
        "    - node-ready\n    - entry-probe", "    - entry-probe\n    - node-ready"
    )
    path.write_text(body, encoding="utf-8")
    selected = settings.model_copy(update={"skills_dir": folder})
    configured = load_sop(selected.skills_path)
    assert configured.steps[-2:] == ("entry-probe", "node-ready")
    with pytest.raises(InspectionConfigError, match="版本"):
        load_sop(selected.skills_path, expected_hash=original.skill.content_hash)
    path.write_text(body.replace("entry-probe", "arbitrary-tool"), encoding="utf-8")
    with pytest.raises(InspectionConfigError, match="登记步骤"):
        load_sop(selected.skills_path)
    settings.inspection_config.write_text(
        MANIFEST.replace("checkout_success_ratio", '"sum(up)"'), encoding="utf-8"
    )
    with pytest.raises(InspectionConfigError, match=r"workloads.0.signal.metric"):
        load_manifest(settings)


@pytest.mark.parametrize("scenario", ["healthy", "mixed", "missing", "truncated"])
def test_read_only_collectors_use_raw_samples_and_exact_scope(settings, monkeypatch, scenario):
    checks = plan_checks(load_manifest(settings), load_sop(settings.skills_path))
    calls = []

    def read_workload(name, namespace, *, _request_timeout):
        assert (name, namespace) == ("checkout", "shop")
        assert 0 < _request_timeout <= 5
        calls.append("kubernetes:checkout")
        return SimpleNamespace(
            metadata=SimpleNamespace(uid="uid-1", generation=2),
            spec=SimpleNamespace(replicas=2),
            status=SimpleNamespace(ready_replicas=2, observed_generation=2),
        )

    def prometheus(request):
        assert request.url.path == "/api/v1/query"
        assert float(request.url.params["time"]) == NOW.timestamp()
        expression = request.url.params["query"]
        calls.append(expression)
        assert expression.endswith("[120s]")
        if expression.startswith("checkout_success_ratio"):
            assert '{service="checkout"}' in expression
            if scenario == "mixed":
                return httpx.Response(503)
        elif expression.startswith("kube_node_status_condition"):
            assert 'node="worker-1"' in expression and 'condition="Ready"' in expression
        else:
            assert 'instance="https://checkout.example"' in expression
            assert 'job="vigil-entry-probe"' in expression

        second = -61 if scenario == "mixed" and expression.startswith("kube_node") else -15
        value = "0" if scenario == "mixed" and expression.startswith("probe_success") else "1"
        result = [{"metric": {}, "values": [[NOW.timestamp() + second, value]]}]
        if expression.startswith("kube_node"):
            if scenario == "missing":
                result = []
            elif scenario == "truncated":
                result = result * 2
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "matrix", "result": result}}
        )

    async def now():
        return NOW

    async def collect():
        kube = KubernetesRead()
        monkeypatch.setattr(
            kube,
            "_clients",
            lambda: (
                None,
                SimpleNamespace(
                    read_namespaced_deployment=read_workload,
                    read_namespaced_stateful_set=None,
                ),
            ),
        )
        async with httpx.AsyncClient(
            base_url="http://prometheus", transport=httpx.MockTransport(prometheus)
        ) as client:
            collector = InspectionCollector(
                kube,
                PrometheusClient(client, max_series=1),
                cluster=settings.cluster,
                runtime_path=settings.runtime_path,
                tool_timeout_seconds=5,
                now=now,
            )
            return [await collector.sample(check, "run-read", 60) for check in checks]

    samples = asyncio.run(collect())
    assert calls[0] == "kubernetes:checkout" and len(calls) == 5
    assert samples[0].sampled_at == NOW and samples[0].evidence["uid"] == "uid-1"
    assert samples[-1].sampled_at == NOW - timedelta(seconds=15)
    if scenario == "healthy":
        assert [sample.status for sample in samples] == ["healthy"] * 4
    elif scenario == "mixed":
        assert [sample.status for sample in samples] == [
            "healthy",
            "insufficient",
            "insufficient",
            "abnormal",
        ]
        assert [sample.reason for sample in samples[1:]] == [
            "collection_failed",
            "stale_sample",
            "threshold_not_met",
        ]
        assert samples[2].sampled_at == NOW - timedelta(seconds=61)
    else:
        assert [sample.status for sample in samples] == [
            "healthy",
            "healthy",
            "insufficient",
            "healthy",
        ]
        assert samples[2].reason == {"missing": "missing_data", "truncated": "truncated"}[scenario]


def _schedule(db, settings, clock, *, hook=None, sample=None, timeout=60):
    async def healthy(_check, _run_id, _remaining):
        return Sample("healthy", None, clock(), {"value": 1})

    store = InspectionStore(db, clock=clock)
    sop = load_sop(settings.skills_path)
    checks = plan_checks(load_manifest(settings), sop)
    return InspectionScheduler(
        store, settings.cluster, 20, timeout, sop, checks, sample or healthy, hook
    )


async def _claim(scheduler, started, owner="worker"):
    return await scheduler.store.claim_due(
        cluster=scheduler.cluster,
        started_at=started,
        interval_seconds=scheduler.interval_seconds,
        timeout_seconds=scheduler.timeout_seconds,
        owner=owner,
        sop_hash=scheduler.sop.skill.content_hash,
        checks=scheduler.checks,
    )


async def _summary(store, session_id):
    events = await store.sessions.list_events(session_id)
    return [event.payload for event in events if event.type == "inspection_summary"][-1]


def test_schedule_uses_one_cluster_slot_and_paginates_immutable_history(
    isolated_serve_dsn, settings
):
    clock = Clock()
    hooks = []

    async def on_observations(scope, observations):
        hooks.append(scope.run.id)
        assert len(observations) == len(scope.run.checks)
        for observation in observations:
            assert observation.status == "healthy"
            assert await scope.store.get_observation(observation.id) == observation
        assert await scope.store.enqueue_investigation(scope.run, "不应创建主线") is None

    async def scenario():
        async with Store.open(isolated_serve_dsn) as db, Store.open(isolated_serve_dsn) as replica:
            await db.migrate()
            left = _schedule(db, settings, clock, hook=on_observations)
            right = _schedule(replica, settings, clock, hook=on_observations)
            sessions = SessionStore(db)
            ordinary = await sessions.create_session(title="人工调查")
            start, other = await asyncio.gather(
                left.store.prepare(left.cluster, 20),
                right.store.prepare(right.cluster, 20),
            )
            clock.advance(19)
            assert await _claim(left, start) is None
            assert await sessions.list_sessions(source="inspection") == []
            clock.advance(1)
            claimed = await asyncio.gather(
                _claim(left, start, "left"), _claim(right, other, "right")
            )
            (first,) = [run for run in claimed if run is not None]
            scheduler = left if first.owner == "left" else right
            clock.advance(20)
            assert await _claim(left, start) is None
            await scheduler.execute(first)
            assert hooks == [first.id]
            summary = await _summary(left.store, first.session_id)
            assert summary["status"] == "completed"
            assert summary["sop_hash"] == left.sop.skill.content_hash
            assert await sessions.list_turns(first.session_id) == []
            assert await _claim(right, other) is None
            clock.advance(20)
            second = await _claim(right, other, "right")
            assert second.scheduled_at == NOW + timedelta(seconds=60)
            await right.execute(second)

            assert [row.id for row in await sessions.list_sessions()] == [ordinary.id]
            (row,) = await sessions.list_sessions(source="inspection", limit=1)
            assert row.id == second.session_id and row.title_origin == "inspection"
            # 老轮次收到迟到的持久回执，更新时间变动也不影响时间分页。
            async with db.transaction() as conn:
                await left.store.record_receipt_result(
                    conn,
                    run_id=first.id,
                    session_id=first.session_id,
                    result={"receipt": "verified"},
                )
            cursor = f"{row.created_at.isoformat()}|{row.id}"
            (previous,) = await sessions.list_sessions(source="inspection", before=cursor)
            assert previous.id == first.session_id
            assert (await _summary(left.store, first.session_id))["result"] == {
                "receipt": "verified"
            }
            with pytest.raises(ValueError, match="游标"):
                await sessions.list_sessions(source="inspection", before="invalid")

    asyncio.run(scenario())


def test_timeout_fences_investigation_and_preserves_samples(isolated_serve_dsn, settings):
    clock = Clock()

    async def sample(check, _run, _remaining):
        if check.ordinal == 1:
            raise RuntimeError("测试采集异常")
        if check.ordinal == 2:
            return Sample("abnormal", "threshold_not_met", clock(), {"value": 0})
        return Sample("healthy", None, clock(), {"value": 1})

    stopped = []

    async def on_observations(scope, observations):
        store, run = scope.store, scope.run
        assert [item.status for item in observations] == [
            "healthy",
            "insufficient",
            "abnormal",
            "healthy",
        ]
        assert observations[1].reason == "collection_failed"
        with pytest.raises(SessionClosed):
            await store.sessions.enqueue_input(run.session_id, "用户输入", display_text="用户输入")
        original = observations[2]
        with pytest.raises(ValueError, match="不属于本轮"):
            await scope.resample(run.checks[2].model_copy(update={"target": {"name": "other"}}))
        fresh = await scope.resample(run.checks[2])
        assert fresh.id != original.id
        assert await store.get_observation(original.id) == original
        assert (await _summary(store, run.session_id))["observations"][2]["id"] == fresh.id
        turn = await store.enqueue_investigation(run, "核对节点")
        assert turn == await store.enqueue_investigation(run, "只允许同一主线")
        assert await store.sessions.list_runnable_main() == []
        assert await store.sessions.acquire_main_lease(run.session_id, "ordinary-scanner") is None
        assert await store.sessions.acquire_side_lease(run.session_id, "ordinary-scanner") is None
        lease = await store.claim_investigation(run, "inspection-driver")
        assert (await store.sessions.start_main_turn(lease)).state == "running"
        clock.advance(60)
        with pytest.raises(LeaseLost):
            await store.sessions.renew_lease(lease)
        with pytest.raises(LeaseLost):
            await store.sessions.start_main_turn(lease)
        with pytest.raises(LeaseLost):
            await store.record_result(run, {"late": True})
        stopped.append(turn.id)
        await scope.resample(run.checks[2])

    async def scenario():
        async with Store.open(isolated_serve_dsn) as db:
            await db.migrate()
            scheduler = _schedule(db, settings, clock, hook=on_observations, sample=sample)
            start = await scheduler.store.prepare(scheduler.cluster, 20)
            clock.advance(20)
            run = await _claim(scheduler, start)
            await scheduler.execute(run)
            summary = await _summary(scheduler.store, run.session_id)
            assert summary["status"] == "timed_out"
            assert len(summary["observations"]) == 4 and summary["result"] == {}
            (turn_id,) = stopped
            assert (await scheduler.store.sessions.get_turn(turn_id)).state == "aborted"
            assert await scheduler.store.sessions.list_runnable_main() == []

    asyncio.run(scenario())


def test_restart_reconciles_lost_owner_and_times_out_unfinished_checks(
    isolated_serve_dsn, settings
):
    clock = Clock()

    async def scenario():
        async with Store.open(isolated_serve_dsn) as crashed, Store.open(isolated_serve_dsn) as db:
            await crashed.migrate()
            old = _schedule(crashed, settings, clock, timeout=600)
            started = await old.store.prepare(old.cluster, 20)
            clock.advance(20)
            run = await _claim(old, started, "crashed")
            first = await old.store.save_observation(
                run, run.checks[0], Sample("healthy", None, clock(), {"value": 1})
            )
            restarted = _schedule(db, settings, clock, timeout=60)
            clock.advance(LEASE_SECONDS - 1)
            restarted_at = await restarted.store.prepare(restarted.cluster, 20)
            assert await _claim(restarted, restarted_at, "restarted") is None
            assert (await restarted.store.get_run(run.id)).status == "running"
            clock.advance(2)
            assert await _claim(restarted, restarted_at, "restarted") is None
            summary = await _summary(restarted.store, run.session_id)
            assert summary["status"] == "interrupted"
            assert summary["observations"][0]["id"] == first.id
            assert {item["reason"] for item in summary["observations"][1:]} == {"lease_expired"}
            assert (
                await restarted.store.sessions.get_session(run.session_id)
            ).state == "interrupted"
            with pytest.raises(LeaseLost):
                await old.store.save_observation(
                    run, run.checks[1], Sample("abnormal", None, clock(), {})
                )
            wrong_owner = replace(run, owner="invalid")
            with pytest.raises(LeaseLost):
                await old.store.remaining(wrong_owner)

            # 失联前的时点不补跑，下一节拍创建新轮次。
            clock.advance(18)
            assert await _claim(restarted, restarted_at, "restarted") is None
            clock.advance(1)
            next_run = await _claim(restarted, restarted_at, "restarted")
            assert next_run is not None and next_run.generation > run.generation
            with pytest.raises(LeaseLost):
                await restarted.store.remaining(replace(next_run, generation=run.generation))
            item = await restarted.store.save_observation(
                next_run, next_run.checks[0], Sample("abnormal", "threshold_not_met", clock(), {})
            )
            clock.advance(61)
            assert await _claim(restarted, restarted_at, "restarted") is None
            latest = await _summary(restarted.store, next_run.session_id)
            assert latest["status"] == "timed_out" and latest["observations"][0]["id"] == item.id
            assert {row["reason"] for row in latest["observations"][1:]} == {"timeout"}
            with pytest.raises(LeaseLost):
                await restarted.store.enqueue_investigation(next_run, "已超时")

    asyncio.run(scenario())


def test_installed_wheel_reads_the_same_inspection_sop(built_wheel, tmp_path, settings):
    import os
    import subprocess
    import sys

    target = tmp_path / "installed"
    subprocess.run(
        ["uv", "pip", "install", "--target", str(target), "--no-deps", str(built_wheel)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    checked = subprocess.run(
        [
            sys.executable,
            "-c",
            "from aiops.config import Settings; "
            "from aiops.inspection.config import load_sop; "
            "s = Settings(_env_file=None); "
            "sop = load_sop(s.skills_path); "
            "print(sop.skill.path); print(sop.skill.content_hash)",
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(target)},
        check=True,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
    )
    path, digest = checked.stdout.splitlines()
    assert Path(path) == target / "aiops" / "_builtin_skills" / SOP_FILE
    source = load_sop(settings.skills_path)
    assert digest == source.skill.content_hash
    assert Path(path).read_bytes() == source.skill.path.read_bytes()


def test_background_scheduler_cancels_hook_when_deadline_passes(
    isolated_serve_dsn, settings, monkeypatch
):
    clock = Clock()

    async def scenario():
        async with Store.open(isolated_serve_dsn) as db:
            await db.migrate()
            stop = asyncio.Event()
            prepared = asyncio.Event()
            scheduled = asyncio.Event()
            cancelled = asyncio.Event()
            never_returns = asyncio.Event()
            runs = []

            async def hook(scope, observations):
                runs.append(scope.run)
                assert all(item.status == "healthy" for item in observations)
                scheduled.set()
                try:
                    await never_returns.wait()
                finally:
                    cancelled.set()

            scheduler = _schedule(db, settings, clock, hook=hook)
            prepare = scheduler.store.prepare

            async def prepare_and_wait(cluster, interval):
                started = await prepare(cluster, interval)
                prepared.set()
                await schedule_ready.wait()
                return started

            schedule_ready = asyncio.Event()
            monkeypatch.setattr(scheduler.store, "prepare", prepare_and_wait)
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(scheduler.drive(stop, poll_seconds=0))
                try:
                    async with asyncio.timeout(5):
                        await prepared.wait()
                        assert (
                            await scheduler.store.sessions.list_sessions(source="inspection") == []
                        )
                        clock.advance(20)
                        schedule_ready.set()
                        await scheduled.wait()
                        clock.advance(60)
                        await cancelled.wait()
                finally:
                    stop.set()
                    schedule_ready.set()

            (run,) = runs
            summary = await _summary(scheduler.store, run.session_id)
            assert summary["status"] == "timed_out"
            assert len(summary["observations"]) == 4

    asyncio.run(scenario())


_CLAIM_PROCESS = """
import asyncio
import os
import sys
from datetime import datetime, timedelta

from aiops.db import Store
from aiops.inspection.store import InspectionStore

async def main():
    start = datetime.fromisoformat(sys.argv[1])
    async with Store.open(os.environ["VIGIL_DATABASE_URL"]) as db:
        inspections = InspectionStore(db, clock=lambda: start + timedelta(seconds=20))
        print("ready", flush=True)
        run = await inspections.claim_due(
            cluster="vigil-lab", started_at=start, interval_seconds=20, timeout_seconds=60,
            owner=sys.argv[2], sop_hash="competition-test", checks=(),
        )
        print(run.id if run else "skipped", flush=True)

asyncio.run(main())
"""


def test_two_processes_can_claim_only_one_inspection_slot(isolated_serve_dsn):
    import os
    import sys

    async def scenario():
        async with Store.open(isolated_serve_dsn) as db:
            await db.migrate()
            store = InspectionStore(db, clock=Clock())
            start = await store.prepare("vigil-lab", 20)

            async def contender(owner):
                return await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-c",
                    _CLAIM_PROCESS,
                    start.isoformat(),
                    owner,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env={**os.environ, "VIGIL_DATABASE_URL": isolated_serve_dsn},
                )

            processes = []
            try:
                async with asyncio.timeout(20):
                    async with db.transaction() as conn:
                        await conn.execute("SELECT * FROM inspection_schedules FOR UPDATE")
                        for owner in ("first", "second"):
                            processes.append(await contender(owner))
                        for process in processes:
                            assert (await process.stdout.readline()).strip() == b"ready"
                    lines = await asyncio.gather(*(process.communicate() for process in processes))
                    assert all(process.returncode == 0 for process in processes)
            finally:
                for process in processes:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()

            outputs = [stdout.decode().strip() for stdout, _ in lines]
            assert outputs.count("skipped") == 1
            (run_id,) = [output for output in outputs if output != "skipped"]
            run = await store.get_run(run_id)
            assert run.scheduled_at == NOW + timedelta(seconds=20)
            assert len(await store.sessions.list_sessions(source="inspection")) == 1

    asyncio.run(scenario())
