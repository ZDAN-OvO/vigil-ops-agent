"""会话读取、浏览器授权与动作关闭边界回归。"""

import asyncio
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from actions_support import FakeAdapter, FakePrometheus, build_executor, rollout_plan
from conftest import ActionCleanup

from aiops.actions.rollout_undo import ActionError
from aiops.actions.safety import ActionSafety
from aiops.agent.conversation import ConversationDriver, ConversationResult, ConversationRun
from aiops.config import Settings
from aiops.contracts import AttemptStatus, ProposalStatus
from aiops.db import Store
from aiops.serve import drive_queue
from aiops.sessions.store import SessionStore
from aiops.web_api import BrowserAuth, build_web_app


def _client(settings, store, auth, **kwargs):
    app = build_web_app(
        settings,
        sessions=SessionStore(store),
        safety=ActionSafety(store),
        auth=auth,
        **kwargs,
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=settings.web_origin)


async def _login(client, auth, origin):
    link = auth.login_url(origin)
    token = parse_qs(urlsplit(link).fragment)["token"][0]
    page = await client.get("/login")
    assert page.status_code == 200
    assert token not in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    cross = await client.post(
        "/login", data={"token": token}, headers={"Origin": "http://127.0.0.1:12345"}
    )
    assert cross.status_code == 403
    response = await client.post(
        "/login", data={"token": token}, headers={"Origin": origin, "Sec-Fetch-Site": "same-origin"}
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/api" in cookie
    replay = await client.post("/login", data={"token": token}, headers={"Origin": origin})
    assert replay.status_code == 403 and "set-cookie" not in replay.headers
    assert token not in replay.text and token not in str(response.headers)
    return client.cookies["vigil_browser"]


async def _card(store, tmp_path, settings):
    adapter = FakeAdapter(rollout_plan())
    executor = await build_executor(
        store,
        adapter,
        FakePrometheus([200.0, 200.0]),
        tmp_path,
        cleanup=ActionCleanup(),
        group_key="experiment-group",
        web_url=settings.web_url,
    )
    proposal, _, _ = await executor.prepare_rollback(
        deployment="productcatalogservice", reason="验证回滚", actor="tester"
    )
    card = await executor.publish_test_card(proposal.id)
    return executor, proposal, card, adapter


def test_create_session_requires_browser_login_and_can_accept_first_message(isolated_serve_dsn):
    async def run():
        async with Store.open(isolated_serve_dsn) as store:
            await store.migrate()
            settings = Settings(_env_file=None)
            auth = BrowserAuth(interactive=True)
            async with _client(settings, store, auth) as client:
                headers = {"Origin": settings.web_origin}
                assert (await client.post("/api/sessions", headers=headers)).status_code == 403
                await _login(client, auth, settings.web_origin)
                assert (await client.post("/api/sessions")).status_code == 403
                created = await client.post("/api/sessions", headers=headers)
                assert created.status_code == 201
                session = created.json()["session"]
                assert session["source"] == "user" and session["state"] == "idle"
                response = await client.post(
                    f"/api/sessions/{session['id']}/messages",
                    headers=headers,
                    json={"text": "checkout 健康吗"},
                )
                assert response.status_code == 202
                assert response.json()["queued_seq"] == 2
                listing = await client.get("/api/sessions")
                assert listing.json()["sessions"][0]["id"] == session["id"]

    asyncio.run(run())


def test_card_decision_requires_exact_host_origin_cookie_and_bound_fingerprint(
    isolated_serve_dsn, tmp_path
):
    async def run():
        settings = Settings(_env_file=None, web_url="http://127.0.0.1:8090")
        origin = settings.web_origin

        async def snapshot():
            return {
                "alerts": {
                    "status": "ok",
                    "missing": [],
                    "failed": [],
                    "data": {
                        "groups": [
                            {"group_key": "experiment-group", "session": None},
                            {"group_key": None, "session": None},
                        ],
                    },
                },
                "warnings": {"data": {"items": [{"message": "token=synthetic-secret"}]}},
            }

        async with Store.open(isolated_serve_dsn) as store:
            await store.migrate()
            executor, proposal, card, adapter = await _card(store, tmp_path, settings)
            session_path = f"/api/sessions/{proposal.session_id}"
            card_path = f"{session_path}/cards/{proposal.id}/decision"
            decision = {"decision": "rejected", "fingerprint": card.payload["fingerprint"]}
            auth = BrowserAuth(interactive=True)
            async with _client(settings, store, auth, monitoring=snapshot) as client:
                other = await SessionStore(store).create_session(title="另一会话")
                assert (
                    await client.get(session_path, headers={"Host": "evil.test"})
                ).status_code == 403
                assert (
                    await client.get(session_path, headers={"Origin": "http://localhost:8090"})
                ).status_code == 403
                assert (
                    await client.post(card_path, json=decision, headers={"Origin": origin})
                ).status_code == 403
                assert (await client.get(session_path)).json()["approval_disabled_reason"]
                cookie = await _login(client, auth, origin)
                response = (await client.get(session_path)).json()
                assert response["approval_enabled"] and response["approval_disabled_reason"] is None
                assert response["session"]["pending_approval"] is True
                events = await client.get(f"{session_path}/events")
                [published] = [
                    event
                    for event in events.json()["events"]
                    if event["type"] == "approval_required"
                ]
                assert (
                    published["url"]
                    == f"{origin}/sessions/{proposal.session_id}#card-{proposal.id}"
                )
                assert cookie not in events.text

                panel = await client.get("/api/monitoring")
                [group, missing] = panel.json()["alerts"]["data"]["groups"]
                assert group["session"] == {
                    "session_id": proposal.session_id,
                    "pending_approval": True,
                }
                assert missing["session"] is None
                assert "synthetic-secret" not in panel.text

                for headers in (
                    {},
                    {"Origin": "null"},
                    {"Origin": "http://127.0.0.1:12345"},
                    {"Origin": origin, "Sec-Fetch-Site": "same-site"},
                    {
                        "Origin": origin,
                        "Host": "evil.test",
                        "X-Forwarded-Host": settings.web_authority,
                    },
                ):
                    denied = await client.post(card_path, json=decision, headers=headers)
                    assert denied.status_code == 403
                assert await store.get_approval(proposal.id) is None
                allowed = {"Origin": origin}
                wrong_session = await client.post(
                    f"/api/sessions/{other.id}/cards/{proposal.id}/decision",
                    headers=allowed,
                    json=decision,
                )
                assert wrong_session.status_code == 404
                missing_fingerprint = await client.post(
                    card_path, headers=allowed, json={"decision": "rejected"}
                )
                assert missing_fingerprint.status_code == 400
                changed = await client.post(
                    card_path, headers=allowed, json={**decision, "fingerprint": "0" * 64}
                )
                assert changed.status_code == 409 and changed.json()["error"] == "hash_mismatch"
                rejected = await client.post(
                    card_path,
                    headers=allowed,
                    json={**decision, "reason": "token=synthetic-secret /home/example/.env"},
                )
                assert rejected.status_code == 200
                assert rejected.json()["status"] == "rejected"
                receipt = await store.get_approval(proposal.id)
                assert receipt.id == rejected.json()["decision_id"]
                assert "synthetic-secret" not in receipt.reason
                assert (await client.post(card_path, json=decision, headers=allowed)).json()[
                    "error"
                ] == "not_pending"
                assert not (await client.get(session_path)).json()["session"]["pending_approval"]
                assert adapter.calls == []

                message = {"text": "请继续查，token=synthetic-secret", "input_id": "web-input-1"}
                queued = await client.post(
                    f"{session_path}/messages", headers=allowed, json=message
                )
                assert queued.status_code == 202
                same = await client.post(f"{session_path}/messages", headers=allowed, json=message)
                assert same.json() == queued.json()
                events = (await client.get(f"{session_path}/events")).json()["events"]
                [visible] = [event for event in events if event.get("input_id") == "web-input-1"]
                assert "synthetic-secret" not in visible["text"]

            async with _client(settings, store, BrowserAuth(interactive=False)) as client:
                headers = {"Origin": origin, "Cookie": f"vigil_browser={cookie}"}
                assert (await client.get("/login")).status_code == 403
                assert (await client.get(session_path, headers=headers)).json()[
                    "approval_enabled"
                ] is False
                for path in (
                    card_path,
                    f"{session_path}/close",
                    f"{session_path}/messages",
                    f"{session_path}/resume",
                ):
                    assert (
                        await client.post(path, headers=headers, json=decision)
                    ).status_code == 403

    asyncio.run(run())


class _SenderStopped(BaseException):
    """模拟进程在保存发送权后退出。"""


@pytest.mark.parametrize("point", ["pending", "dry_run", "send_granted", "crash_after_grant"])
def test_manual_alert_close_serializes_with_card_and_send(
    isolated_serve_dsn, tmp_path, monkeypatch, point
):
    async def run():
        settings = Settings(_env_file=None)
        ready, proceed = asyncio.Event(), asyncio.Event()
        original = ActionSafety.grant_send

        async def hold_send(self, reservation, attempt, approval):
            if point == "crash_after_grant":
                await original(self, reservation, attempt, approval)
                ready.set()
                raise _SenderStopped
            if point == "send_granted":
                granted = await original(self, reservation, attempt, approval)
                ready.set()
                await proceed.wait()
                return granted
            ready.set()
            await proceed.wait()
            return await original(self, reservation, attempt, approval)

        if point != "pending":
            monkeypatch.setattr(ActionSafety, "grant_send", hold_send)
        async with Store.open(isolated_serve_dsn) as store:
            await store.migrate()
            executor, proposal, card, adapter = await _card(store, tmp_path, settings)
            path = f"/api/sessions/{proposal.session_id}"
            auth = BrowserAuth(interactive=True)
            async with _client(settings, store, auth) as client:
                await _login(client, auth, settings.web_origin)
                origin = {"Origin": settings.web_origin}
                decision = {"decision": "approved", "fingerprint": card.payload["fingerprint"]}
                running = None
                if point != "pending":
                    approved = await client.post(
                        f"{path}/cards/{proposal.id}/decision", headers=origin, json=decision
                    )
                    assert approved.status_code == 200
                    running = asyncio.create_task(executor.execute_authorized(proposal.id))
                try:
                    if running:
                        await asyncio.wait_for(ready.wait(), 5)
                    for _ in range(2):
                        closed = await client.post(f"{path}/close", headers=origin)
                        assert closed.status_code == 200
                        assert closed.json()["session"]["state"] == "closed"
                        assert not closed.json()["session"]["pending_approval"]
                    for endpoint in ("messages", "resume", f"cards/{proposal.id}/decision"):
                        closed = await client.post(
                            f"{path}/{endpoint}", headers=origin, json=decision
                        )
                        assert (
                            closed.status_code == 409 and closed.json()["error"] == "session_closed"
                        )
                finally:
                    proceed.set()
                    if point == "dry_run":
                        with pytest.raises(ActionError):
                            await running
                    elif point == "crash_after_grant":
                        with pytest.raises(_SenderStopped):
                            await running
                    elif running:
                        assert (await running).status == AttemptStatus.VERIFIED
                if point == "crash_after_grant":
                    async with Store.open(isolated_serve_dsn) as new_process:
                        async with new_process.transaction() as conn:
                            await conn.execute(
                                "UPDATE action_locks SET expires_at = clock_timestamp() "
                                "WHERE proposal_id = %s",
                                (proposal.id,),
                            )
                        restarted_safety = ActionSafety(new_process)
                        await restarted_safety.recover_expired_targets()
                        await restarted_safety.recover_expired_targets()
                events = (await client.get(f"{path}/events")).json()["events"]
                [closed_event] = [event for event in events if event["type"] == "session_closed"]
                assert closed_event["reason"] == "manual"
                final = await store.get_proposal(proposal.id)
                if point == "send_granted":
                    assert final.status == ProposalStatus.SUCCEEDED and adapter.calls == [
                        True,
                        False,
                    ]
                    assert events[-1]["type"] == "action_result" and events[-1]["written"] is True
                    assert events[-1]["seq"] > closed_event["seq"]
                elif point == "crash_after_grant":
                    assert final.status == ProposalStatus.UNKNOWN and adapter.calls == [True]
                    assert (await store.get_attempt(proposal.id)).status == AttemptStatus.UNKNOWN
                    assert events[-1]["type"] == "action_result" and events[-1]["written"] is None
                    assert events[-1]["seq"] > closed_event["seq"]
                    assert [event.get("status") for event in events].count("unknown") == 1
                else:
                    assert final.status == ProposalStatus.VOIDED
                    assert adapter.calls == ([True] if point == "dry_run" else [])
                    if point == "dry_run":
                        assert (await store.get_approval(proposal.id)).revoked_at is not None
                        assert (await store.get_attempt(proposal.id)).status == AttemptStatus.VOIDED

    asyncio.run(run())


def test_decided_card_waits_for_terminal_lease_and_is_scanned_after_restart(
    isolated_serve_dsn, tmp_path, monkeypatch
):
    async def run():
        from investigation_support import FakeToolbox

        from aiops import serve

        monkeypatch.setattr(serve, "_SCAN_INTERVAL", 0.03)
        settings = Settings(_env_file=None)
        processed = asyncio.Event()
        observed = []

        async def finish(self):
            # 只替代模型与工具执行，扫描、卡片事务、领取和轮次提交均使用真实库。
            current = await safety.get_card_decision(proposal.id, session_id=proposal.session_id)
            observed.append(current.id)
            async with sessions.run(self.lease, proposal.session_turn_id) as tx:
                result = await tx.append_event("result", {"status": "completed"})
                await tx.set_turn_state("completed")
            processed.set()
            return ConversationResult(result, None)

        monkeypatch.setattr(ConversationRun, "drive", finish)
        async with Store.open(isolated_serve_dsn) as terminal:
            await terminal.migrate()
            executor, proposal, card, _ = await _card(terminal, tmp_path, settings)
            path = f"/api/sessions/{proposal.session_id}/cards/{proposal.id}/decision"
            async with Store.open(isolated_serve_dsn) as browser:
                auth = BrowserAuth(interactive=True)
                async with _client(settings, browser, auth) as client:
                    await _login(client, auth, settings.web_origin)
                    decision = await client.post(
                        path,
                        headers={"Origin": settings.web_origin},
                        json={"decision": "rejected", "fingerprint": card.payload["fingerprint"]},
                    )
                    assert decision.status_code == 200
                assert await ActionSafety(browser).list_runnable_card_sessions() == []
            sessions = SessionStore(terminal)
            assert await sessions.release_lease(executor._session_lease)
            async with Store.open(isolated_serve_dsn) as restarted:
                safety = ActionSafety(restarted)
                assert await safety.list_runnable_card_sessions() == [proposal.session_id]
                driver = ConversationDriver(
                    settings=settings, store=restarted, toolbox=FakeToolbox()
                )
                sessions = driver.sessions
                stop = asyncio.Event()
                worker = asyncio.create_task(drive_queue(driver, safety, stop))
                try:
                    await asyncio.wait_for(processed.wait(), timeout=5)
                    assert observed == [decision.json()["decision_id"]]
                    assert await safety.list_runnable_card_sessions() == []
                    assert (await sessions.get_session(proposal.session_id)).state == "idle"
                finally:
                    stop.set()
                    await worker

    asyncio.run(run())
