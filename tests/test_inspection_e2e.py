"""通过 serve 组装的巡检计划、调查、写入和公开回放。"""

import asyncio
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb
from test_inspection_remediation import TARGET, ReviewModel, Workload
from test_inspection_run import Clock
from test_remediation_integration import CONFIG, Metrics
from test_remediation_store import _rows
from test_web_api import _client, _login

from aiops import serve
from aiops.actions import remediation_service
from aiops.actions.remediation_store import RemediationStore
from aiops.agent.engine import LangChainInvestigation
from aiops.config import Settings
from aiops.db import Store
from aiops.inspection.collector import InspectionCollector
from aiops.inspection.models import Sample
from aiops.inspection.store import InspectionStore
from aiops.investigation import factory
from aiops.web_api import BrowserAuth, build_web_app

MANIFEST = """
version: 1
cluster: vigil-lab
workloads:
  - namespace: vigil-experiment
    kind: Deployment
    name: vigil-replica-fixture
    readiness_max_age_seconds: 60
    signal:
      metric: vigil_replica_fixture_rejected
      labels: {job: vigil-replica-state}
      minimum: 1
      max_age_seconds: 60
nodes:
  - name: worker
    max_age_seconds: 60
"""
SECRET = "sk-private-inspection-sample"
PATH = "C:/vigil-local/fixture/inspection.yaml"
INTERVAL = 900
DEADLINE = 40


async def eventually(read, ready=bool):
    async with asyncio.timeout(10):
        while not ready(value := await read()):
            await asyncio.sleep(0.01)
    return value


def install_clock(dsn, now):
    # 仅在用例的隔离 schema 内替换取时函数，保留产品 SQL 和真实事务。
    options = conninfo_to_dict(dsn)["options"]
    dsn = make_conninfo(dsn, options=f"{options},pg_catalog")
    with psycopg.connect(dsn) as conn:
        conn.execute("CREATE TABLE inspection_test_clock (value timestamptz NOT NULL)")
        conn.execute("INSERT INTO inspection_test_clock VALUES (%s)", (now,))
        schema = conn.execute("SELECT current_schema()").fetchone()[0]
        for name in ("clock_timestamp", "now"):
            conn.execute(
                sql.SQL(
                    "CREATE FUNCTION {}() RETURNS timestamptz LANGUAGE sql VOLATILE "
                    "AS $$ SELECT value FROM {} $$"
                ).format(sql.Identifier(name), sql.Identifier(schema, "inspection_test_clock"))
            )
        assert conn.execute("SELECT clock_timestamp(), now()").fetchone() == (now, now)
    return dsn


class Probe(Metrics):
    def __init__(self):
        self.healthy = False

    async def raw_samples(self, selector, start, end):
        return [
            (at, int(self.healthy)) for at, _ in await super().raw_samples(selector, start, end)
        ]


class Model(ReviewModel):
    async def ainvoke(self, messages):
        reply = await super().ainvoke(messages)
        reply.tool_calls[0]["args"]["explanation"] += f" {SECRET} {PATH}"
        return reply


class Scenario:
    def __init__(self):
        self.clock = Clock(datetime.now(UTC))
        self.kube = Workload()
        self.metrics = Probe()
        self.model = Model()
        self.sample_mode = "healthy"
        self.blocked = asyncio.Event()

    async def sample(self, check, _run_id, _remaining):
        if check.check_id == "node-ready":
            if self.sample_mode == "timeout":
                self.blocked.set()
                await asyncio.Future()
            if self.sample_mode == "insufficient":
                return Sample.insufficient(f"collection_failed {SECRET} {PATH}", raw_ref=PATH)
            return Sample("healthy", None, self.clock(), {})
        if check.check_id == "workload-readiness":
            target = self.kube.target("vigil-lab", *TARGET.values())
            return Sample("healthy", None, self.clock(), {"ready_replicas": 1, "uid": target.uid})
        abnormal = self.sample_mode == "abnormal"
        return Sample(
            "abnormal" if abnormal else "healthy", None, self.clock(), {"value": int(not abnormal)}
        )

    def use_fixtures(self, monkeypatch):
        @asynccontextmanager
        async def read_clients(_settings):
            yield None, None

        def ledger(store, safety):
            return RemediationStore(
                store, safety, inspections=InspectionStore(store, clock=self.clock)
            )

        async def sample(_collector, *args):
            return await self.sample(*args)

        monkeypatch.setattr(factory, "_open_read_clients", read_clients)
        monkeypatch.setattr(InspectionCollector, "sample", sample)
        monkeypatch.setattr(LangChainInvestigation, "build_model", lambda _settings: self.model)
        monkeypatch.setattr(remediation_service, "RemediationStore", ledger)
        monkeypatch.setattr(
            remediation_service.RemediationKubeAdapter,
            "from_kubeconfig",
            lambda **_kwargs: self.kube,
        )
        monkeypatch.setattr(remediation_service, "RemediationMetrics", lambda _client: self.metrics)

    async def advance(self, ledger, seconds):
        self.clock.advance(seconds)
        async with ledger.store.transaction() as conn:
            await conn.execute("UPDATE inspection_test_clock SET value = %s", (self.clock(),))

    async def step(self, ledger, mode, seconds=INTERVAL):
        self.sample_mode = mode
        await self.advance(ledger, seconds)

    @staticmethod
    async def rounds(ledger, count, *, status=None):
        async def read():
            return await _rows(ledger, "SELECT * FROM inspection_runs ORDER BY scheduled_at")

        return await eventually(
            read,
            lambda rows: len(rows) == count and (status is None or rows[-1]["status"] == status),
        )

    @staticmethod
    async def drive_due(runtime):
        ledger = runtime.ledger
        [identifier] = await eventually(ledger.due)
        await runtime.drive(identifier)
        return identifier


async def summaries(client, session_id):
    after_seq = 0
    snapshots = []
    while True:
        response = await client.get(
            f"/api/sessions/{session_id}/events", params={"limit": 2, "after_seq": after_seq}
        )
        assert response.status_code == 200
        assert SECRET not in response.text and PATH not in response.text
        page = response.json()
        snapshots.extend(item for item in page["events"] if item["type"] == "inspection_summary")
        if not page["has_more"]:
            return snapshots
        assert page["next_after_seq"] > after_seq
        after_seq = page["next_after_seq"]


async def exercise_lifecycle(state, runtime, web):
    ledger = runtime.ledger
    await eventually(lambda: _rows(ledger, "SELECT * FROM inspection_schedules"))
    assert not await ledger.sessions.list_sessions(source="inspection")

    await state.step(ledger, "healthy")
    [healthy] = await state.rounds(ledger, 1, status="completed")
    last = (await summaries(web, healthy["session_id"]))[-1]
    assert [item["status"] for item in last["observations"]] == ["healthy"] * 3
    assert not state.model.calls and not state.kube.sent

    await state.step(ledger, "abnormal")
    first = await state.drive_due(runtime)
    abnormal = (await state.rounds(ledger, 2, status="completed"))[-1]
    pending = (await summaries(web, abnormal["session_id"]))[-1]
    assert pending["result"]["actions"][first]["status"] == "applied"
    assert pending["result"]["investigation"]["status"] == "reviewed"
    assert pending["result"]["rules"][-1]["status"] == "proposal_created"
    assert state.kube.sent == [{"replicas": 2}]
    [review] = await _rows(
        ledger,
        "SELECT r.observation_ids, f.workload_uid FROM remediation_inspection_reviews r "
        "JOIN remediation_faults f ON f.id = r.fault_id",
    )
    [candidate] = state.model.calls[0]["candidates"]
    assert review["workload_uid"] == candidate["target_uid"]
    assert set(review["observation_ids"]) == set(candidate["observation_ids"])
    assert set(review["observation_ids"]) <= {item["id"] for item in pending["observations"]}

    # 写入后就绪时间须晚于触发它的异常观察。
    await state.advance(ledger, 1)
    assert await state.drive_due(runtime) == first
    assert not await ledger.due()
    await state.advance(ledger, 619)
    assert await state.drive_due(runtime) == first
    unchanged = (await summaries(web, abnormal["session_id"]))[-1]
    assert unchanged["seq"] > pending["seq"] and unchanged["status"] == "completed"
    assert unchanged["result"]["actions"][first]["outcome"] == "no_improvement"

    await state.step(ledger, "abnormal", INTERVAL - 620)
    persistent = (await state.rounds(ledger, 3, status="completed"))[-1]
    assert persistent["result"]["rules"][-1]["status"] == "fault_already_handled"
    [fault] = await _rows(ledger, "SELECT * FROM remediation_faults")
    assert fault["cleared_at"] is None
    assert len(state.model.calls) == 2 and len(state.kube.sent) == 1

    state.metrics.healthy = True
    await state.step(ledger, "healthy")
    recovered = (await state.rounds(ledger, 4, status="completed"))[-1]
    assert recovered["result"]["faults"][0]["status"] == "cleared"
    assert len(state.model.calls) == 2 and len(state.kube.sent) == 1

    await state.step(ledger, "abnormal")
    second = await state.drive_due(runtime)
    recurrence = (await state.rounds(ledger, 5, status="completed"))[-1]
    before = (await summaries(web, recurrence["session_id"]))[-1]
    assert second != first and len(state.kube.sent) == 2
    await state.advance(ledger, 1)
    assert await state.drive_due(runtime) == second
    await state.advance(ledger, 619)
    assert await state.drive_due(runtime) == second
    after = (await summaries(web, recurrence["session_id"]))[-1]
    assert after["seq"] > before["seq"]
    assert after["result"]["actions"][second]["outcome"] == "improved"
    faults = await _rows(ledger, "SELECT * FROM remediation_faults ORDER BY opened_at")
    assert [item["cleared_at"] is not None for item in faults] == [True, False]
    assert after["result"]["actions"][second]["fault_id"] == faults[1]["id"]
    jobs = await _rows(
        ledger, "SELECT proposal_id, state, sent_at FROM remediation_jobs ORDER BY sent_at"
    )
    assert [(job["proposal_id"], job["state"]) for job in jobs] == [
        (first, "finished"),
        (second, "finished"),
    ]
    assert all(job["sent_at"] is not None for job in jobs)

    await state.step(ledger, "timeout", INTERVAL - 620)
    await state.rounds(ledger, 6)
    async with asyncio.timeout(5):
        await state.blocked.wait()
    await state.advance(ledger, DEADLINE)
    timed_out = (await state.rounds(ledger, 6, status="timed_out"))[-1]
    timeout_summary = (await summaries(web, timed_out["session_id"]))[-1]
    assert timeout_summary["reason"] == "deadline_exceeded"
    assert any(item.get("reason") == "timeout" for item in timeout_summary["observations"])

    await state.step(ledger, "insufficient", INTERVAL - DEADLINE)
    insufficient = (await state.rounds(ledger, 7, status="completed"))[-1]
    last = (await summaries(web, insufficient["session_id"]))[-1]
    incomplete = [item for item in last["observations"] if item["status"] == "insufficient"]
    assert len(incomplete) == 1 and "collection_failed" in incomplete[0]["reason"]
    assert len(state.model.calls) == 3 and len(state.kube.sent) == 2
    assert last["result"]["faults"][0]["status"] == "cleared"
    assert not await _rows(ledger, "SELECT * FROM remediation_alerts")
    return await state.rounds(ledger, 7)


def test_serve_drives_inspection_and_reopens_history(isolated_serve_dsn, monkeypatch, tmp_path):
    manifest = tmp_path / "inspection.yaml"
    manifest.write_text(MANIFEST, encoding="utf-8")
    state = Scenario()
    dsn = install_clock(isolated_serve_dsn, state.clock())
    settings = Settings(
        _env_file=None,
        database_url=dsn,
        inspection_enabled=True,
        inspection_config=manifest,
        inspection_interval_seconds=INTERVAL,
        inspection_timeout_seconds=DEADLINE,
        action_kubeconfig=tmp_path / "test-write.kubeconfig",
        remediation_config_dir=CONFIG / "isolated-acceptance",
        remediation_auto_enabled=True,
        tool_timeout_seconds=1,
        model_name="test-review",
    )

    async def run():
        state.use_fixtures(monkeypatch)
        recorded = []

        async def listen(driver, safety, settings, *, interactive, remediation, inspection):
            assert inspection.store is remediation.ledger.inspections
            assert interactive and settings.webhook_host is None
            stop = asyncio.Event()
            app = build_web_app(
                settings,
                sessions=driver.sessions,
                safety=safety,
                auth=BrowserAuth(interactive=True),
            )
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(inspection.drive(stop, poll_seconds=0.01))
                try:
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url=settings.web_origin
                    ) as web:
                        recorded.extend(await exercise_lifecycle(state, remediation, web))
                finally:
                    stop.set()

        monkeypatch.setattr(serve, "require_web_assets", lambda: None)
        monkeypatch.setattr(serve, "_listen", listen)
        await serve.run_serve(settings, interactive=True)
        return recorded

    recorded = asyncio.run(run())
    expected_order = asyncio.run(check_reopened_history(settings, recorded))
    check_cli_history(dsn, tmp_path, recorded[-1]["session_id"], expected_order)


async def check_reopened_history(settings, recorded):
    async with Store.open(settings.database_url) as store:
        async with store.transaction() as conn:
            rows = await conn.execute(
                "SELECT id, created_at FROM sessions WHERE source = 'inspection'"
            )
            sessions = await rows.fetchall()
        expected_order = [
            row["id"]
            for row in sorted(
                sessions, key=lambda row: (row["created_at"], row["id"]), reverse=True
            )
        ]
        assert sorted(expected_order) == sorted(row["session_id"] for row in recorded)
        auth = BrowserAuth(interactive=True)
        async with _client(settings, store, auth) as web:
            await _login(web, auth, settings.web_origin)
            headers = {"Origin": settings.web_origin}
            created = await web.post("/api/sessions", headers=headers)
            assert created.status_code == 201
            manual = created.json()["session"]["id"]
            default = (await web.get("/api/sessions")).json()
            assert [item["id"] for item in default["sessions"]] == [manual]
            assert "next_before" not in default

            before = None
            read = []
            for _ in range(4):
                params = {"limit": 2, "source": "inspection"}
                if before is not None:
                    params["before"] = before
                response = await web.get("/api/sessions", params=params)
                assert response.status_code == 200
                page = response.json()
                assert all(item["source"] == "inspection" for item in page["sessions"])
                read.extend(item["id"] for item in page["sessions"])
                before = page["next_before"]
                if before is not None:
                    last = page["sessions"][-1]
                    assert before == f"{last['created_at']}|{last['id']}"
            assert before is None
            assert read == expected_order
            for invalid in (
                "?source=inspection&before=bad",
                "?source=inspection&before=2026-11-20T02:14:00%7Cid",
                "?source=inspection&before=2026-11-20%7Cid",
                "?source=inspection&source=inspection",
                "?source=other",
                "?before=bad",
            ):
                assert (await web.get("/api/sessions" + invalid)).status_code == 400

            session_id = recorded[-1]["session_id"]
            snapshots = await summaries(web, session_id)
            snapshot = snapshots[-1]
            observation = next(
                row for row in snapshot["observations"] if row["status"] == "insufficient"
            )
            # 模拟升级前的旧摘要直接含有采集路径、模型明文和任意内部数据。
            observation["reason"] = f"collection_failed {SECRET} {PATH}"
            observation["evidence_refs"] += [f"file://{PATH}"]
            observation["evidence"] = {"unlabelled": "private-evidence-body"}
            snapshot["result"]["investigation"] = {
                "explanation": f"password=fixture-plaintext {PATH} {SECRET}",
                "provider_blob": "private-model-body",
            }
            async with store.transaction() as conn:
                await conn.execute(
                    "UPDATE session_events SET payload = %s WHERE session_id = %s AND seq = %s",
                    (Jsonb(snapshot), session_id, snapshot["seq"]),
                )
            latest = (await summaries(web, session_id))[-1]
            assert latest["seq"] == snapshot["seq"]
            assert latest["status"] == "completed"
            assert "[已遮盖]" in str(latest)
            for value in ("fixture-plaintext", "private-model-body", "private-evidence-body"):
                assert value not in str(latest)
            for row in latest["observations"]:
                assert row["evidence_refs"] == [f"inspection:{row['id']}"]

            path = f"/api/sessions/{session_id}"
            detail = (await web.get(path)).json()
            assert detail["session"]["source"] == "inspection" and not detail["approval_enabled"]
            for endpoint, body in (
                ("/messages", {"text": "人工输入", "input_id": uuid4().hex}),
                ("/title", {"title": "手工标题"}),
                ("/resume", None),
                ("/close", None),
                ("/cards/unknown/decision", {"fingerprint": "unknown", "decision": "approved"}),
            ):
                rejected = await web.post(path + endpoint, headers=headers, json=body)
                assert rejected.status_code == 409
                assert rejected.json()["error"] == "inspection_read_only"
            assert (await summaries(web, session_id))[-1] == latest
    return expected_order


def check_cli_history(dsn, tmp_path, session_id, expected_order):
    env = {
        **os.environ,
        "VIGIL_DATABASE_URL": dsn,
        "VIGIL_MODEL_NAME": "",
        "VIGIL_RUNTIME_DIR": str(tmp_path / "runtime"),
        "PYTHONUTF8": "1",
    }

    def cli(*args, code=0):
        result = subprocess.run(
            [sys.executable, "-m", "aiops.cli", *args],
            env=env,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            check=False,
        )
        assert result.returncode == code, result.stderr
        for value in (
            SECRET,
            PATH,
            "fixture-plaintext",
            "private-model-body",
            "private-evidence-body",
        ):
            assert value not in result.stdout + result.stderr
        return result.stdout

    default = cli("sessions")
    assert "\tuser\t" in default and "\tinspection\t" not in default
    history = cli("sessions", "--source", "inspection")
    assert [line.split("\t")[0] for line in history.splitlines()] == expected_order
    events = [
        json.loads(line)
        for line in cli("--resume", session_id, "--output", "stream-json").splitlines()
    ]
    summaries = [item for item in events if item["type"] == "inspection_summary"]
    assert len(summaries) > 1
    assert events[-1]["status"] == "read_only"
    assert (
        json.loads(cli("--resume", session_id, "--output", "json"))["inspection_summary"]
        == summaries[-1]
    )
    recent = [json.loads(line) for line in cli("-c", "--output", "stream-json").splitlines()]
    assert recent[0]["source"] == "user" and recent[0]["session_id"] != session_id
    assert "巡检会话仅供回查" in cli(
        "-p", "人工输入", "--resume", session_id, "--output", "json", code=1
    )
