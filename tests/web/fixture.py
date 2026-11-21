"""使用真实会话事件外壳及出站脱敏的同源网页夹具。"""

import json
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from mimetypes import guess_type
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, unquote, urlsplit

from aiops.sessions.models import SessionEvent
from aiops.sessions.projection import project_payload, project_template_diff
from aiops.web_api import PAGE_CSP

STATIC = Path(__file__).resolve().parents[2] / "web" / "dist"
WHEN = datetime(2026, 11, 20, 2, 14, tzinfo=UTC)
SECRET = "fictional-fixture-secret-value"
LOCAL_PATH = "/home/fixture/private-run/raw.json"
CARD = "act-web"
SESSIONS = {
    "alert-demo": ("alert", "首页探测失败", "awaiting_approval"),
    "idle-demo": ("user", "日常值班", "idle"),
    "working-demo": ("user", "查看资源用量", "working"),
    "interrupted-demo": ("user", "接手续查", "interrupted"),
    "closed-demo": ("alert", "已恢复的告警", "closed"),
}


def block(data, *, missing=()):
    return {
        "status": "partial" if missing else "ok",
        "data": data,
        "missing": list(missing),
        "failed": [],
    }


def monitoring():
    return {
        "collected_at": WHEN.isoformat(),
        "abnormal_service_count": 1,
        "grafana_links": {},
        "alerts": block(
            {
                "groups": [
                    {
                        "name": "FrontendProbeFailed",
                        "group_key": '{}:{alertname="FrontendProbeFailed"}',
                        "receiver": "vigil",
                        "alerts": [
                            {
                                "fingerprint": "alert-fixture",
                                "duration_seconds": 180,
                                "severity": "critical",
                                "starts_at": WHEN.isoformat(),
                                "suppressed": False,
                            }
                        ],
                        "session": {"session_id": "alert-demo", "pending_approval": True},
                    }
                ]
            }
        ),
        "probes": block(
            {
                "step_seconds": 15,
                "applications": [
                    {
                        "application": "Online Boutique",
                        "targets": [
                            {
                                "address": "https://store.invalid/cart",
                                "state": "down",
                                "availability": 0.67,
                                "duration_seconds": 0.3,
                                "http_status": 503,
                                "sample_count": 3,
                                "points": [[1, 1], [16, None], [31, 0], [46, 1]],
                            }
                        ],
                    }
                ],
            }
        ),
        "changes": block(
            {
                "items": [
                    {
                        "namespace": "public-fixture",
                        "service": "frontend",
                        "version": "r3",
                        "kind": "vigil_rollback",
                        "occurred_at": WHEN.isoformat(),
                        "approved_by": "值班员",
                    }
                ]
            }
        ),
        "services": block(
            {
                "abnormal_count": 1,
                "abnormal": [
                    {
                        "name": "adservice",
                        "kind": "Deployment",
                        "issues": [
                            {
                                "kind": "frequent_restarts",
                                "restart_count": 3,
                                "restart_count_last_hour": 3,
                                "occurred_at": WHEN.isoformat(),
                            },
                            {
                                "kind": "resource_near_limit",
                                "container": "app",
                                "resource": "memory",
                                "usage": 107 * 1048576,
                                "limit": 128 * 1048576,
                                "unit": "bytes",
                                "ratio": 107 / 128,
                            },
                        ],
                        "resource_status": "ok",
                        "last_exits": [
                            {
                                "reason": "OOMKilled",
                                "exit_code": 137,
                                "occurred_at": WHEN.isoformat(),
                            }
                        ],
                        "missing": [],
                    }
                ],
                "healthy": [{"name": "checkoutservice", "kind": "Deployment"}],
                "unknown": [],
            }
        ),
        "warnings": block(
            {
                "items": [
                    {
                        "reason": "BackOff",
                        "object": {"kind": "Pod", "name": "adservice-demo"},
                        "message": f"退避重启 token={SECRET}，采样文件 {LOCAL_PATH}",
                        "count": 3,
                        "last_seen": WHEN.isoformat(),
                    }
                ]
            }
        ),
        "nodes": block(
            {
                "healthy": ["node-b"],
                "abnormal": [
                    {"name": "node-a", "conditions": [{"condition": "Ready", "status": "false"}]}
                ],
                "unknown": [],
            }
        ),
    }


class Fixture:
    session_id = "alert-demo"

    def __init__(self):
        self.sessions = SESSIONS.copy()
        self.events = {key: [] for key in SESSIONS}
        self.states = {key: state for key, (_, _, state) in SESSIONS.items()}
        self.approval_enabled = True
        self.snapshot = monitoring()
        self.query_cursors = []
        self.posts = []
        self.reject_reads = False
        self.delay_decision = False
        self.duplicate_events = False
        self.pending_decision = None
        self.push("session", source="alert", title="首页探测失败", resumed=False)
        self.push("message", source="alert", role="user", text="首页探测持续失败")
        for n in range(6):
            self.push(
                "tool_call",
                tool="k8s_pod_status",
                summary=f"确认 Pod 就绪情况 {n}",
                status="ok",
                collected_at=WHEN.isoformat(),
                observation_id=f"obs-{n}",
                observation={"excerpt": "观测明细" * 110, "source": LOCAL_PATH, "token": SECRET},
            )
        self.push("hypothesis", id="h1", statement="坏发布导致首页探测失败", status="supported")
        self.push("evidence", evidence_id="e-1", observation_id="obs-0", hypothesis_id="h1")
        self.push("message", role="user", input_id="hint-main", text="检查上一版镜像")
        self.push("input_delivered", input_id="hint-main", step=6, lane="main")
        self.push("message", role="user", input_id="hint-side", text="现在几个副本？")
        self.push("input_delivered", input_id="hint-side", step=2, lane="side")
        self.push(
            "approval_required",
            card_id=CARD,
            fingerprint="fixture-fingerprint",
            action="rollout_undo",
            status="pending_approval",
            target={
                "cluster": "demo",
                "namespace": "public-fixture",
                "kind": "Deployment",
                "name": "frontend",
            },
            parameters={"replicas": 2},
            risk={"disposition": "approval", "reasons": ["需要核对版本"]},
            reason="上一次发布后探测失败",
            verification={"verification_plan": "检查首页探针与 Deployment 就绪副本"},
            evidence_ids=["e-1", "e-unavailable"],
            template_diff=[
                {
                    "op": "replace",
                    "path": "/spec/containers/0/image",
                    "current": "app:r4",
                    "value": "app:r3",
                },
                {"op": "remove", "path": "/spec/containers/0/resources/limits/cpu", "current": "2"},
                *project_template_diff(
                    [{"op": "replace", "path": "/spec/containers/0/env/0/value", "value": SECRET}]
                ),
            ],
        )

    def push(self, kind, sid=None, **payload):
        sid = sid or self.session_id
        log = self.events[sid]
        seq = len(log) + 1
        event = SessionEvent(
            kind, sid, seq, WHEN + timedelta(seconds=seq), "turn-demo", project_payload(payload)
        ).as_dict()
        log.append(event)
        return event

    def decision(self, status):
        return self.push(
            "card_decision",
            card_id=CARD,
            status=status,
            decision_id="decision-demo",
            actor="值班员",
            reason="",
            expires_at=(datetime.now(UTC) + timedelta(minutes=2)).isoformat()
            if status == "approved"
            else None,
        )

    def receipt(self, proposal, attempt=None, **payload):
        return self.push(
            "action_result",
            card_id=CARD,
            proposal_status=proposal,
            status=attempt or proposal,
            attempt_id="attempt-demo" if attempt else None,
            phase=payload.pop("phase", "intent"),
            written=payload.pop("written", False),
            verification=payload.pop("verification", None),
            **payload,
        )

    def session(self, sid):
        source, title, _ = self.sessions[sid]
        state = self.states[sid]
        return {
            "id": sid,
            "source": source,
            "title": title,
            "state": state,
            "last_seq": len(self.events[sid]),
            "created_at": WHEN.isoformat(),
            "updated_at": WHEN.isoformat(),
            "closed_at": WHEN.isoformat() if state == "closed" else None,
            "pending_approval": state == "awaiting_approval"
            and not any(
                event["type"] in {"card_decision", "action_result"} for event in self.events[sid]
            ),
        }

    def access(self):
        return {
            "approval_enabled": self.approval_enabled,
            "approval_disabled_reason": None if self.approval_enabled else "浏览器缺少批准凭证",
        }

    def start(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def send(self, status, data, *, mime="application/json", page=False):
                content = (
                    data
                    if isinstance(data, bytes)
                    else json.dumps(data, ensure_ascii=False).encode()
                )
                self.send_response(status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(content)))
                if page:
                    self.send_header("Content-Security-Policy", PAGE_CSP)
                    if fixture.approval_enabled:
                        self.send_header(
                            "Set-Cookie", "fixture_access=public; Path=/; SameSite=Strict; HttpOnly"
                        )
                self.end_headers()
                self.wfile.write(content)

            def error(self, status, code, message):
                self.send(status, {"error": code, "message": message})

            def do_GET(self):
                url = urlsplit(self.path)
                parts = url.path.strip("/").split("/")
                if url.path == "/grafana-fixture":
                    self.send(200, b"<title>Grafana fixture</title>", mime="text/html")
                elif url.path == "/" or (parts[0] == "sessions" and len(parts) == 2):
                    self.send(
                        200,
                        (STATIC / "index.html").read_bytes(),
                        mime="text/html; charset=utf-8",
                        page=True,
                    )
                elif url.path.startswith("/static/"):
                    path = (STATIC / unquote(url.path.removeprefix("/static/"))).resolve()
                    if path.is_relative_to(STATIC) and path.is_file():
                        self.send(
                            200,
                            path.read_bytes(),
                            mime=guess_type(path.name)[0] or "application/octet-stream",
                        )
                    else:
                        self.error(404, "not_found", "资源不存在")
                elif url.path == "/api/sessions":
                    self.send(
                        200,
                        {
                            "sessions": [fixture.session(sid) for sid in fixture.sessions.copy()],
                            **fixture.access(),
                        },
                    )
                elif url.path == "/api/monitoring":
                    self.send(200, project_payload(fixture.snapshot))
                elif parts[:2] == ["api", "sessions"] and len(parts) in {3, 4}:
                    sid = unquote(parts[2])
                    if sid not in fixture.sessions:
                        self.error(404, "not_found", "会话不存在")
                    elif len(parts) == 3:
                        self.send(200, {"session": fixture.session(sid), **fixture.access()})
                    elif parts[3] == "events":
                        query = parse_qs(url.query)
                        after = int(query["after_seq"][0])
                        limit = int(query["limit"][0])
                        fixture.query_cursors.append(after)
                        if fixture.reject_reads:
                            self.error(503, "offline", "夹具暂时断开")
                            return
                        log = fixture.events[sid]
                        events = log[after : after + limit]
                        next_seq = events[-1]["seq"] if events else after
                        if fixture.duplicate_events and after:
                            events = [log[after - 1], *events]
                        self.send(
                            200,
                            {
                                "events": events,
                                "next_after_seq": next_seq,
                                "has_more": len(log) > next_seq,
                            },
                        )
                else:
                    self.error(404, "not_found", "资源不存在")

            def do_POST(self):
                if (
                    not fixture.approval_enabled
                    or self.headers.get("Cookie") != "fixture_access=public"
                    or self.headers.get("Origin") != fixture.url
                ):
                    self.error(403, "read_only", "浏览器缺少批准凭证")
                    return
                parts = urlsplit(self.path).path.strip("/").split("/")
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if parts == ["api", "sessions"]:
                    if raw:
                        self.error(400, "unexpected_body", "新会话请求不带正文")
                        return
                    sid = "created-demo"
                    fixture.posts.append(("create", None))
                    fixture.events[sid] = []
                    fixture.states[sid] = "idle"
                    fixture.sessions[sid] = ("user", "新会话", "idle")
                    self.send(201, {"session": fixture.session(sid)})
                    return
                sid = unquote(parts[2])
                action = parts[3]
                body = json.loads(raw) if raw else {}
                fixture.posts.append((action, body))
                if action == "cards" and parts[4:] == [CARD, "decision"]:
                    if body.get("fingerprint") != "fixture-fingerprint":
                        self.error(409, "stale", "卡片已经变化")
                        return
                    fixture.pending_decision = body["decision"]
                    if not fixture.delay_decision:
                        fixture.decision(body["decision"])
                    self.send(200, {"decision_id": "decision-demo", "status": body["decision"]})
                elif action == "messages":
                    event = fixture.push("message", sid=sid, role="user", **body)
                    self.send(202, {"input_id": event["input_id"], "queued_seq": event["seq"]})
                elif action == "resume":
                    fixture.states[sid] = "working"
                    self.send(202, {"session": fixture.session(sid)})
                elif action == "close":
                    fixture.receipt("voided")
                    fixture.states[sid] = "closed"
                    fixture.push("session_closed", sid=sid, reason="operator")
                    self.send(200, {"session": fixture.session(sid)})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.task = Thread(target=self.server.serve_forever, daemon=True)
        self.task.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.task.join()
