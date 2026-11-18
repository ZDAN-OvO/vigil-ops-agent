"""本机 Web 契约、出站清理和快照隔离。"""

import asyncio
from datetime import UTC, datetime

import pytest
from starlette.testclient import TestClient
from test_web_store import DSN, seed_execution, seed_work
from test_web_store import web_dsn as web_dsn

from aiops.config import Settings
from aiops.contracts import (
    ActionFollowup,
    AttemptOutcome,
    AttemptStatus,
    Hypothesis,
    ProposalStatus,
)
from aiops.db import Store
from aiops.web.app import build_web_app
from aiops.webhook import build_app

URL = "http://127.0.0.1:8080"
ORIGIN = {"Origin": URL}
FAKE_TEXT = "token=FAKE-vigil-key-77 日志位于 /tmp/private/vigil/secret.txt"


FAKE_TOKEN = FAKE_TEXT.split()[0].removeprefix("token=")
# 带前缀的凭据键、私钥键和 URI 形式的本机路径也必须遮掉。
FAKE_TEXT += (
    " DB_PASSWORD=fake-db-pw-41 SSL_PRIVATE_KEY: fake-ssl-key-42 file:/home/fake-user/x.json"
)
FAKE_VALUES = (FAKE_TOKEN, "/tmp/private", "fake-db-pw-41", "fake-ssl-key-42", "/home/fake-user")


def test_local_origin_headers_and_api_not_found(tmp_path):
    settings = Settings(_env_file=None)
    with TestClient(build_web_app(settings, None, dist=tmp_path), base_url=URL) as client:
        for path, headers, status, code in [
            ("/api", {"Host": "remote.test:8080"}, 421, "host_rejected"),
            ("/api/conversations", {"Origin": "http://remote.test"}, 403, "origin_rejected"),
            ("/api/conversations", {}, 403, "origin_rejected"),
            ("/api/conversations", ORIGIN, 422, "invalid_request"),
            ("/api/missing", ORIGIN, 404, "not_found"),
        ]:
            res = client.request("POST", path, headers=headers, json={"input": FAKE_TEXT})
            assert res.status_code == status
            assert res.json()["code"] == code
            assert res.headers["content-security-policy"] == "frame-ancestors 'none'"
            assert res.headers["x-frame-options"] == "DENY"
            assert FAKE_TOKEN not in res.text
            assert "/tmp/private" not in res.text
        page = client.get("/")
        assert page.status_code == 503 and "make web-build" in page.text
        assert page.headers["x-frame-options"] == "DENY"


@pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL")
def test_api_status_codes_and_response_filtering(web_dsn, monkeypatch):
    from aiops.web import app as web_app

    async def scenario():
        import httpx

        settings = Settings(database_url=web_dsn, model_name="test")
        async with Store.open(web_dsn) as store:
            await store.migrate()
            api = build_web_app(settings, store)
            transport = httpx.ASGITransport(app=api, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url=URL) as client:
                blank = await client.get("/api/conversations/current")
                assert blank.json()["conversation"] is None
                create = {"request_id": "create", "service": "frontend", "symptoms": FAKE_TEXT}
                res = await client.post("/api/conversations", headers=ORIGIN, json=create)
                assert res.status_code == 201
                conv_id = res.json()["id"]
                replay = await client.post("/api/conversations", headers=ORIGIN, json=create)
                assert replay.status_code == 200 and replay.json() == res.json()
                duplicate = await client.post(
                    "/api/conversations", headers=ORIGIN, json=create | {"request_id": "other"}
                )
                assert duplicate.status_code == 409
                assert duplicate.json()["code"] == "conversation_active"
                for request_id, text, status in [
                    ("hint", FAKE_TEXT, 201),
                    ("hint", FAKE_TEXT, 200),
                    ("hint", "更改", 409),
                ]:
                    msg = await client.post(
                        f"/api/conversations/{conv_id}/messages",
                        headers=ORIGIN,
                        json={"request_id": request_id, "content": text},
                    )
                    assert msg.status_code == status
                data = await store.get_current_conversation()
                await store.save_hypothesis(
                    Hypothesis(
                        id="hyp-test",
                        investigation_id=data["investigation_id"],
                        statement=FAKE_TEXT,
                        status="supported",
                    )
                )
                snapshot = await client.get("/api/conversations/current")
                assert snapshot.status_code == 200
                assert len(snapshot.json()["messages"]) == 2
                assert snapshot.json()["evidence"][0]["hypothesis_id"] == "hyp-test"
                assert not [v for v in FAKE_VALUES if v in snapshot.text]

                def fail(_):
                    raise RuntimeError(FAKE_TEXT)

                monkeypatch.setattr(web_app, "list_services", fail)
                unavailable = await client.get("/api/services")
                assert unavailable.status_code == 503
                assert unavailable.json()["code"] == "cluster_unavailable"
                assert FAKE_TOKEN not in unavailable.text

                async def fail_snapshot(*_):
                    raise RuntimeError(FAKE_TEXT)

                monkeypatch.setattr(web_app, "read_snapshot", fail_snapshot)
                internal = await client.get("/api/conversations/current")
                assert internal.status_code == 500
                assert internal.json()["code"] == "internal_error"
                assert FAKE_TOKEN not in internal.text
                assert "/tmp/private" not in internal.text

    asyncio.run(scenario())


@pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL")
def test_alert_app_does_not_serve_web_routes(web_dsn):
    with TestClient(build_app(Settings(database_url=web_dsn))) as client:
        assert client.get("/health").status_code == 200
        for route in ("/", "/api", "/api/conversations/current", "/assets/app.js"):
            assert client.get(route).status_code == 404


@pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL")
def test_latest_card_and_outcome_do_not_hide_previous_followups(web_dsn):
    from actions_support import make_proposal

    from aiops.contracts import RiskDecision, RiskDisposition
    from aiops.web.snapshot import read_snapshot

    async def scenario():
        settings = Settings(database_url=web_dsn)
        async with Store.open(web_dsn) as store:
            await store.migrate()
            _, investigation, _, first = await seed_work(store, proposal_status="executing")
            attempt = await seed_execution(store, first, "sent")
            followup = ActionFollowup(
                id="fol-first",
                proposal_id=first.id,
                attempt_id=attempt.id,
                cluster=first.target.cluster,
                namespace=first.target.namespace,
                target_kind=first.target.kind,
                target_name=first.target.name,
                outcome=AttemptOutcome.UNKNOWN,
                reason=FAKE_TEXT,
                evidence={},
                created_at=datetime.now(UTC),
            )
            await store.finish_attempt(
                attempt.id,
                proposal_id=first.id,
                proposal_status=ProposalStatus.UNKNOWN,
                status=AttemptStatus.UNKNOWN,
                result={"verification_done": True},
                verification=None,
                finished_at=datetime.now(UTC),
                followup=followup,
            )
            second = make_proposal(
                id="act-second",
                idempotency_key="second",
                investigation_id=investigation.id,
                reason=FAKE_TEXT,
                created_at=datetime.now(UTC),
            )
            await store.save_proposal(
                second,
                RiskDecision(
                    proposal_id=second.id,
                    disposition=RiskDisposition.APPROVAL,
                    policy_version="test",
                    reasons=[FAKE_TEXT],
                    requires_compensation=False,
                ),
            )
            await store.set_task_waiting(investigation.id, proposal_id=second.id)
            async with store._pool.connection() as conn:
                await conn.execute(
                    "UPDATE investigation_tasks SET ready_proposal_id = %s "
                    "WHERE investigation_id = %s",
                    (second.id, investigation.id),
                )
            snap = await read_snapshot(store, settings)
            assert snap.proposal.id == second.id
            assert snap.proposal.card_hash
            assert snap.outcome is None
            assert snap.followups[0].proposal_id == first.id
            assert snap.conversation.page_state == "needs_verification"
            encoded = snap.model_dump_json()
            assert not [v for v in FAKE_VALUES if v in encoded]
            await store.resolve_followup(followup.id, actor="tester", note="已核实")
            assert (
                await read_snapshot(store, settings)
            ).conversation.page_state == "waiting_decision"

    asyncio.run(scenario())


@pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL")
def test_sse_replays_only_events_after_last_event_id(web_dsn):
    from test_web_store import new_investigation

    async def scenario():
        settings = Settings(database_url=web_dsn)
        async with Store.open(web_dsn) as store:
            await store.migrate()
            created = await store.create_conversation("create", *new_investigation())
            await store.append_conversation_message("hint", created.id, FAKE_TEXT)
            first, second = await store.list_conversation_events(created.id)
            app = build_web_app(settings, store)
            frames = []
            disconnected = asyncio.Event()

            async def receive():
                await disconnected.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                frames.append(message)
                if message.get("body", b"").startswith(b"id:"):
                    disconnected.set()

            path = f"/api/conversations/{created.id}/events"
            await asyncio.wait_for(
                app(
                    {
                        "type": "http",
                        "method": "GET",
                        "path": path,
                        "root_path": "",
                        "headers": [
                            (b"host", b"127.0.0.1:8080"),
                            (b"last-event-id", str(first["id"]).encode()),
                        ],
                        "query_string": b"",
                        "asgi": {"version": "3.0", "spec_version": "2.3"},
                        "scheme": "http",
                        "server": ("127.0.0.1", 8080),
                    },
                    receive,
                    send,
                ),
                timeout=5,
            )
            payload = b"".join(m.get("body", b"") for m in frames)
            assert f"id: {second['id']}\n".encode() in payload
            assert f"id: {first['id']}\n".encode() not in payload
            assert FAKE_TOKEN.encode() not in payload

    asyncio.run(scenario())


@pytest.mark.skipif(not DSN, reason="未配置 VIGIL_DATABASE_URL")
def test_open_followup_resolved_during_running_investigation(web_dsn):
    from aiops.web.snapshot import read_snapshot

    async def scenario():
        settings = Settings(database_url=web_dsn)
        async with Store.open(web_dsn) as store:
            await store.migrate()
            _, _, _, proposal = await seed_work(store, proposal_status="executing")
            attempt = await seed_execution(store, proposal, "sent")
            followup = ActionFollowup(
                id="fol-running",
                proposal_id=proposal.id,
                attempt_id=attempt.id,
                cluster=proposal.target.cluster,
                namespace=proposal.target.namespace,
                target_kind=proposal.target.kind,
                target_name=proposal.target.name,
                outcome=AttemptOutcome.UNKNOWN,
                reason="待核实",
                evidence={},
                created_at=datetime.now(UTC),
            )
            await store.finish_attempt(
                attempt.id,
                proposal_id=proposal.id,
                proposal_status=ProposalStatus.UNKNOWN,
                status=AttemptStatus.UNKNOWN,
                result={"stage": "done", "verification_done": True},
                verification=None,
                finished_at=datetime.now(UTC),
                followup=followup,
            )
            pending = await read_snapshot(store, settings)
            assert pending.conversation.page_state == "needs_verification"
            await store.resolve_followup(followup.id, actor="tester", note="已核实")
            snapshot = await read_snapshot(store, settings)
            assert snapshot.conversation.page_state == "investigating"
            assert not snapshot.conversation.finished
            assert snapshot.followups[0].resolution == "已核实"

    asyncio.run(scenario())


def test_redaction_keeps_digest_only_in_card_image_and_handles_long_input():
    import time

    from aiops.web.redaction import Redactor

    clean = Redactor(Settings(_env_file=None))
    digest = "sha256:" + "1" * 64
    assert "1" * 64 not in clean(f"plain {digest}")
    image = f"gcr.io/demo/app@{digest}"
    card = clean.change({"path": "containers[server].image", "from": "demo:canary", "to": image})
    assert image in card["after"]
    other = clean.change({"path": "containers[server].args", "from": None, "to": digest})
    assert "1" * 64 not in other["after"]
    pem = "-----BEGIN PRIVATE KEY-----\nMIIfakekeybody\n-----END PRIVATE KEY-----"
    assert "MIIfakekeybody" not in clean(pem)
    assert "MIIfakekeybody" not in clean("private_key: " + pem)
    assert "fake-bearer-value" not in clean("Authorization: Bearer fake-bearer-value")
    assert "fake-spaced-value" not in clean("api key: fake-spaced-value")
    known = Redactor(Settings(_env_file=None, model_api_key="registry-secret-xyz"))
    leaked = known.change(
        {
            "path": "containers[server].image",
            "from": None,
            "to": f"registry-secret-xyz/app@{digest}",
        }
    )
    assert "registry-secret-xyz" not in leaked["after"] and "1" * 64 in leaked["after"]
    started = time.monotonic()
    clean("-" * 8000 + "=:" * 4000)
    assert time.monotonic() - started < 0.5
