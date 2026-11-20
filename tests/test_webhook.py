"""双监听使用真实 HTTP 与隔离的 PostgreSQL，模型和集群操作使用替身。"""

import asyncio
import re
import socket
from contextlib import asynccontextmanager, contextmanager
from secrets import token_urlsafe
from time import monotonic

import httpx
import pytest
from investigation_support import FakeToolbox, ScriptedChatModel, report_call

from aiops import serve
from aiops.agent.conversation import ConversationDriver
from aiops.config import Settings
from aiops.db import Store
from aiops.investigation import factory
from aiops.sessions.store import SessionStore

WEBHOOK_PATH = "/webhook/alertmanager"
GROUP_KEY = '{}/{alertname="PodOOMKilled"}:{alertname="PodOOMKilled"}'


def _alert(name, status, pod):
    return {
        "status": status,
        "labels": {"alertname": name, "severity": "warning", "pod": pod, "scenario": "hidden"},
        "annotations": {"summary": f"{pod} 发生内存异常"},
    }


def _payload(status, states):
    return {
        "groupKey": GROUP_KEY,
        "status": status,
        "alerts": [
            _alert("PodOOMKilled", states[0], "checkout-1"),
            _alert("ContainerOOMKilled", states[1], "checkout-2"),
        ],
    }


def _free_port():
    with socket.create_server(("127.0.0.1", 0)) as sock:
        return sock.getsockname()[1]


class ParkedDriver:
    """第一个进程只接收并确认，以便下一个进程验证数据库中已有的轮次。"""

    def __init__(self, store):
        self.sessions = SessionStore(store)

    @asynccontextmanager
    async def claim(self, session_id, *, lane):
        yield None


@asynccontextmanager
async def _start(settings, monkeypatch, opener, *, interactive):
    started = asyncio.get_running_loop().create_future()

    @contextmanager
    def capture(stop):
        started.set_result(stop)
        yield

    monkeypatch.setattr(factory, "open_conversation", opener, raising=False)
    monkeypatch.setattr(serve, "_shutdown_signals", capture)
    monkeypatch.setattr(serve, "_SCAN_INTERVAL", 0.04)
    task = asyncio.create_task(serve.run_serve(settings, interactive=interactive))
    try:
        done, _ = await asyncio.wait([started, task], return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
        async with (
            httpx.AsyncClient(base_url=settings.web_origin, trust_env=False) as web,
            httpx.AsyncClient(
                base_url=f"http://{settings.webhook_host}:{settings.webhook_port}", trust_env=False
            ) as webhook,
        ):
            for client, path, status in (
                (web, "/api/sessions", 200),
                (webhook, WEBHOOK_PATH, 405),
            ):
                deadline = monotonic() + 5
                while True:
                    if task.done():
                        await task
                    try:
                        result = await client.get(path, timeout=0.25)
                        assert result.status_code == status
                        break
                    except httpx.TransportError:
                        if monotonic() >= deadline:
                            raise
                        await asyncio.sleep(0.02)
            yield web, webhook
    finally:
        if started.done():
            started.result().set()
        await asyncio.wait_for(task, timeout=10)


def test_alertmanager_group_over_tcp_survives_listener_restart(
    isolated_serve_dsn, tmp_path, monkeypatch, capsys, caplog
):
    async def run():
        port = _free_port()
        token = token_urlsafe(32)
        settings = Settings(
            _env_file=None,
            database_url=isolated_serve_dsn,
            web_port=port,
            web_url=f"http://127.0.0.1:{port}",
            webhook_host="127.0.0.2",
            webhook_port=port,
            webhook_token=token,
            model_name="scripted",
            runtime_dir=tmp_path,
        )
        auth = {"Authorization": f"Bearer {token}"}
        firing = _payload("firing", ["firing", "firing"])
        mixed = _payload("firing", ["firing", "resolved"])
        resolved = _payload("resolved", ["resolved", "resolved"])

        @asynccontextmanager
        async def parked(*, settings, store):
            yield ParkedDriver(store)

        async with _start(settings, monkeypatch, parked, interactive=False) as (web, webhook):
            assert (await web.post(WEBHOOK_PATH, headers=auth, json=firing)).status_code == 404
            assert (await webhook.get("/api/sessions")).status_code == 404
            for invalid in ({}, {"X-Vigil-Token": token}, {**auth, "X-Vigil-Token": token}):
                assert (
                    await webhook.post(WEBHOOK_PATH, headers=invalid, json=firing)
                ).status_code == 401
            invalid = await webhook.post(
                WEBHOOK_PATH, headers=auth, json={**firing, "groupKey": ""}
            )
            assert invalid.status_code == 400
            assert invalid.json()["error"] == "invalid_body"

            one, two = await asyncio.gather(
                *[webhook.post(WEBHOOK_PATH, headers=auth, json=firing) for _ in range(2)]
            )
            assert one.status_code == two.status_code == 200
            assert {one.json()["outcome"], two.json()["outcome"]} == {"created", "repeated"}
            old_id = one.json()["session_id"]
            assert old_id == two.json()["session_id"]
            partly = await webhook.post(WEBHOOK_PATH, headers=auth, json=mixed)
            assert partly.json() == {"outcome": "partially_resolved", "session_id": old_id}
            first_page = (await web.get(f"/api/sessions/{old_id}/events?limit=2")).json()
            assert first_page["has_more"]
            next_page = (
                await web.get(
                    f"/api/sessions/{old_id}/events?after_seq={first_page['next_after_seq']}"
                )
            ).json()
            assert [event["seq"] for event in next_page["events"]] == [3, 4]
            assert "部分告警已恢复" in next_page["events"][-1]["text"]
            assert not next_page["has_more"]
            assert "hidden" not in str(first_page)
            whole = await webhook.post(WEBHOOK_PATH, headers=auth, json=resolved)
            assert whole.json() == {"outcome": "closed", "session_id": old_id}
            duplicate = await webhook.post(WEBHOOK_PATH, headers=auth, json=resolved)
            assert duplicate.json() == {"outcome": "ignored", "session_id": None}
            state = (await web.get(f"/api/sessions/{old_id}")).json()
            assert state["session"]["state"] == "closed"
            assert not state["approval_enabled"]
            again = await webhook.post(WEBHOOK_PATH, headers=auth, json=firing)
            new_id = again.json()["session_id"]
            assert again.json()["outcome"] == "created" and new_id != old_id

        assert "网页登录链接" not in capsys.readouterr().out
        async with Store.open(isolated_serve_dsn) as store:
            sessions = SessionStore(store)
            [aborted] = await sessions.list_turns(old_id)
            [queued] = await sessions.list_turns(new_id)
            assert aborted.state == "aborted" and queued.state == "queued"
            first = await sessions.get_input(queued.first_input_id)
            assert first.alert_names == ["PodOOMKilled", "ContainerOOMKilled"]

        model = ScriptedChatModel(
            responses=[report_call(confirmed=False, conclusion="已收到告警。")]
        )

        @asynccontextmanager
        async def capable(*, settings, store):
            yield ConversationDriver(
                settings=settings, store=store, toolbox=FakeToolbox(), model=model
            )

        async with _start(settings, monkeypatch, capable, interactive=True) as (web, webhook):
            deadline = monotonic() + 8
            while True:
                response = await web.get(f"/api/sessions/{new_id}/events")
                events = response.json()["events"]
                if any(event["type"] == "result" for event in events):
                    break
                assert monotonic() < deadline
                await asyncio.sleep(0.04)
            [result] = [event for event in events if event["type"] == "result"]
            assert result["exit_code"] == 0
            assert len(model.seen) == 1
            async with Store.open(isolated_serve_dsn) as store:
                investigation = await store.get_investigation(result["investigation_id"])
                incident = await store.get_incident(investigation.incident_id)
                assert incident.trigger == "alert"
                assert incident.alert_names == first.alert_names
                assert "oom" in {skill.name for skill in investigation.skills}
            output = capsys.readouterr().out
            assert output.count("网页登录链接") == 1
            login_token = re.search(r"/login#token=([A-Za-z0-9_-]+)", output)[1]
            login = await web.post(
                "/login", data={"token": login_token}, headers={"Origin": settings.web_origin}
            )
            assert login.status_code == 303 and login.headers["location"] == "/"
            cached = await web.get(f"/api/sessions/{new_id}")
            assert cached.json()["approval_enabled"] is True
            assert login_token not in response.text
            assert login_token not in caplog.text
            assert token not in caplog.text

    asyncio.run(run())


@pytest.mark.parametrize(
    "host,token",
    [
        ("0.0.0.0", "test-value"),
        ("8.8.8.8", "test-value"),
        ("::", "test-value"),
        ("localhost", "test-value"),
        ("172.20.0.1", None),
        (None, "test-value"),
        ("172.20.0.1", ""),
    ],
)
def test_webhook_listener_rejects_incomplete_or_wildcard_settings(host, token):
    settings = Settings(_env_file=None, webhook_host=host, webhook_token=token)
    with pytest.raises(ValueError, match="VIGIL_WEBHOOK"):
        settings.validate_webhook_listener()


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8090",
        "http://evil.example",
        "http://127.0.0.1:1234",
        "http://127.0.0.1:8090/#token=x",
        "http://user@127.0.0.1:8090",
    ],
)
def test_card_web_url_cannot_point_away_from_web_listener(url):
    with pytest.raises(ValueError, match="VIGIL_WEB_URL"):
        Settings(_env_file=None, web_port=8090, web_url=url)
