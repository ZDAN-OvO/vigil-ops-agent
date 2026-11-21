"""用户本机的会话读取、输入和人工决定。"""

import hmac
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_urlsafe
from typing import Annotated, Any
from urllib.parse import parse_qs

from psycopg.errors import UniqueViolation
from pydantic import BaseModel, StringConstraints, field_validator
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp

from aiops.actions.rollout_undo import ActionError
from aiops.actions.safety import ActionSafety
from aiops.config import PROJECT_ROOT, SOURCE_CHECKOUT, Settings
from aiops.contracts import ApprovalDecision
from aiops.http_api import APIError, http_error, read_body, read_json
from aiops.sessions.models import Session, SessionClosed
from aiops.sessions.projection import project_payload, project_text
from aiops.sessions.store import SessionStore
from aiops.web_assets import STATIC_PREFIX, WebAssetsUnavailable, check_web_assets

_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_COOKIE = "vigil_browser"
_API_LIMIT = 64 * 1024
_MONITORING_BLOCKS = ("alerts", "probes", "changes", "services", "warnings", "nodes")
MonitoringSnapshot = Callable[[], Awaitable[dict[str, Any]]]
STATIC_DIR = PROJECT_ROOT / "web/dist" if SOURCE_CHECKOUT else Path(__file__).with_name("static")
PAGE_CSP = (
    "default-src 'self'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'self'"
)


def _missing_assets_message() -> str:
    if SOURCE_CHECKOUT:
        return "网页产物缺失或不完整，请在仓库运行 make web-build"
    return "网页产物缺失或不完整，请重新安装 wheel，或运行 make web-build 后重新打包"


def require_web_assets() -> None:
    try:
        check_web_assets(STATIC_DIR)
    except (OSError, ValueError):
        raise WebAssetsUnavailable(_missing_assets_message()) from None


class BrowserAuth:
    def __init__(self, *, interactive: bool) -> None:
        self.interactive = interactive
        self._login_token = token_urlsafe(32) if interactive else None
        self._cookie: str | None = None

    def login_url(self, origin: str) -> str | None:
        # 片段不经 HTTP 发送，正文中的兑换凭证也不会出现在访问日志。
        return f"{origin}/login#token={self._login_token}" if self._login_token else None

    def exchange(self, token: str) -> str | None:
        if self._login_token is None or not hmac.compare_digest(
            token.encode(), self._login_token.encode()
        ):
            return None
        self._login_token = None
        self._cookie = token_urlsafe(32)
        return self._cookie

    def disabled_reason(self, request: Request) -> str | None:
        if not self.interactive:
            return "Vigil 以非交互模式启动，网页只读"
        presented = request.cookies.get(_COOKIE, "")
        if self._cookie is None or not hmac.compare_digest(
            presented.encode(), self._cookie.encode()
        ):
            return "请通过启动终端中的一次性链接登录"
        return None


def _check_origin(request: Request, settings: Settings) -> None:
    if request.headers.getlist("origin") != [settings.web_origin]:
        raise APIError(403, "invalid_origin", "请求必须来自当前网页")
    site = request.headers.getlist("sec-fetch-site")
    if site and site != ["same-origin"]:
        raise APIError(403, "invalid_origin", "请求必须来自当前网页")


class _LocalBrowser(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.headers.getlist("host") != [self.settings.web_authority]:
            return await http_error(request, APIError(403, "invalid_host", "网页仅接受本机地址"))
        # 浏览器直接打开网页地址时会使用 none；外站的 API 请求一律拒绝。
        site = request.headers.getlist("sec-fetch-site")
        origin = request.headers.getlist("origin")
        if request.scope["path"].startswith("/api/") and (
            site not in ([], ["none"], ["same-origin"])
            or origin not in ([], [self.settings.web_origin])
        ):
            return await http_error(
                request, APIError(403, "invalid_origin", "请求必须来自当前网页")
            )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


class MessageBody(BaseModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]
    input_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,128}$")] | None = None

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if "\0" in value:
            raise ValueError("消息含无效字符")
        return value


class DecisionBody(BaseModel):
    decision: ApprovalDecision
    fingerprint: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)] = ""

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        if "\0" in value:
            raise ValueError("说明含无效字符")
        return value


def _id(request: Request, name: str) -> str:
    value = request.path_params[name]
    if not _ID.fullmatch(value):
        raise APIError(404, "not_found", "记录不存在")
    return value


def _number(request: Request, name: str, default: int, low: int, high: int) -> int:
    values = request.query_params.getlist(name)
    if not values:
        return default
    if len(values) == 1 and re.fullmatch(r"[0-9]{1,19}", values[0]):
        number = int(values[0])
        if low <= number <= high:
            return number
    raise APIError(400, "invalid_query", "查询参数无效")


def _summary(session: Session, pending: set[str]) -> dict[str, Any]:
    return {
        "id": session.id,
        "source": session.source,
        "title": project_text(session.title),
        "state": session.state,
        "last_seq": session.last_seq,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
        "closed_at": session.closed_at.isoformat() if session.closed_at else None,
        "pending_approval": session.closed_at is None and session.id in pending,
    }


def _empty_monitoring_snapshot() -> dict[str, Any]:
    return {
        "collected_at": datetime.now(UTC).isoformat(),
        "abnormal_service_count": None,
        "grafana_links": {},
        **{
            block: {"status": "missing", "data": None, "missing": ["source"], "failed": []}
            for block in _MONITORING_BLOCKS
        },
    }


def build_web_app(
    settings: Settings,
    *,
    sessions: SessionStore,
    safety: ActionSafety,
    auth: BrowserAuth,
    monitoring: MonitoringSnapshot | None = None,
) -> Starlette:
    async def page(request: Request) -> FileResponse:
        if "session_id" in request.path_params:
            _id(request, "session_id")
        index = STATIC_DIR / "index.html"
        if not index.is_file():
            raise APIError(503, "web_assets_missing", _missing_assets_message())
        return FileResponse(index, headers={"Content-Security-Policy": PAGE_CSP})

    async def require_session(request: Request, *, open_only: bool = False) -> Session:
        session = await sessions.get_session(_id(request, "session_id"))
        if session is None:
            raise APIError(404, "not_found", "会话不存在")
        if open_only and session.closed_at is not None:
            raise APIError(409, "session_closed", "会话已关闭")
        return session

    def require_write(request: Request) -> None:
        _check_origin(request, settings)
        if reason := auth.disabled_reason(request):
            raise APIError(403, "approval_disabled", reason)

    async def login_page(_request: Request) -> HTMLResponse:
        if not auth.interactive:
            raise APIError(403, "approval_disabled", "Vigil 以非交互模式启动，网页只读")
        nonce = token_urlsafe(16)
        return HTMLResponse(
            f"""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><title>Vigil 登录</title>
<p>请从 Vigil 启动终端打开一次性登录链接。</p>
<script nonce="{nonce}">
const token = new URLSearchParams(location.hash.slice(1)).get("token");
history.replaceState(null, "", "/login");
if (token) {{
    const form = document.createElement("form");
    form.method = "POST";
    form.action = "/login";
    const input = document.createElement("input");
    input.type = "hidden";
    input.name = "token";
    input.value = token;
    form.append(input);
    document.body.append(form);
    form.submit();
}}
</script></html>""",
            headers={
                "Referrer-Policy": "same-origin",
                "Content-Security-Policy": (
                    f"default-src 'none'; script-src 'nonce-{nonce}'; "
                    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
                ),
            },
        )

    async def login(request: Request) -> RedirectResponse:
        _check_origin(request, settings)
        body = await read_body(request, media_type="application/x-www-form-urlencoded", limit=512)
        try:
            tokens = parse_qs(body.decode("ascii"), max_num_fields=1).get("token", [])
        except ValueError:
            tokens = []
        cookie = auth.exchange(tokens[0]) if len(tokens) == 1 else None
        if cookie is None:
            raise APIError(403, "login_invalid", "登录链接无效或已使用")
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(_COOKIE, cookie, path="/api", httponly=True, samesite="strict")
        return response

    async def list_sessions(request: Request) -> JSONResponse:
        limit = _number(request, "limit", 50, 1, 200)
        rows = await sessions.list_sessions(limit=limit)
        pending = await safety.pending_card_sessions([row.id for row in rows])
        reason = auth.disabled_reason(request)
        return JSONResponse(
            {
                "sessions": [_summary(row, pending) for row in rows],
                "approval_enabled": reason is None,
                "approval_disabled_reason": reason,
            }
        )

    async def create_session(request: Request) -> JSONResponse:
        require_write(request)
        session = await sessions.create_session(title="新会话")
        return JSONResponse({"session": _summary(session, set())}, status_code=201)

    async def session_snapshot(request: Request) -> JSONResponse:
        session = await require_session(request)
        pending = await safety.pending_card_sessions([session.id])
        reason = auth.disabled_reason(request)
        return JSONResponse(
            {
                "session": _summary(session, pending),
                "approval_enabled": reason is None,
                "approval_disabled_reason": reason,
            }
        )

    async def list_events(request: Request) -> JSONResponse:
        session = await require_session(request)
        after_seq = _number(request, "after_seq", 0, 0, 2**63 - 1)
        limit = _number(request, "limit", 100, 1, 200)
        events = await sessions.list_events(session.id, after_seq=after_seq, limit=limit + 1)
        page = events[:limit]
        return JSONResponse(
            {
                "events": [project_payload(event.as_dict()) for event in page],
                "next_after_seq": page[-1].seq if page else after_seq,
                "has_more": len(events) > limit,
            }
        )

    async def send_message(request: Request) -> JSONResponse:
        require_write(request)
        session = await require_session(request, open_only=True)
        body = await read_json(request, MessageBody, limit=_API_LIMIT)
        try:
            incoming = await sessions.enqueue_input(
                session.id, body.text, display_text=project_text(body.text), input_id=body.input_id
            )
        except SessionClosed:
            raise APIError(409, "session_closed", "会话已关闭") from None
        except (ValueError, UniqueViolation):
            raise APIError(409, "input_conflict", "输入 ID 已绑定其它输入") from None
        return JSONResponse(
            {"input_id": incoming.id, "queued_seq": incoming.queued_seq}, status_code=202
        )

    async def resume(request: Request) -> JSONResponse:
        require_write(request)
        session = await require_session(request, open_only=True)
        # 驱动者独立扫描已持久化的轮次，恢复请求不会重放或创建轮次。
        pending = await safety.pending_card_sessions([session.id])
        return JSONResponse({"session": _summary(session, pending)}, status_code=202)

    async def close(request: Request) -> JSONResponse:
        require_write(request)
        session = await require_session(request)
        if session.group_key is None:
            raise APIError(409, "not_alert_session", "仅告警会话可以关闭")
        async with sessions.alert_transaction(session.group_key) as conn:
            await safety.close_session(conn, session.id, reason="manual")
        result = await require_session(request)
        return JSONResponse({"session": _summary(result, set())})

    async def decide(request: Request) -> JSONResponse:
        require_write(request)
        session = await require_session(request, open_only=True)
        card_id = _id(request, "card_id")
        body = await read_json(request, DecisionBody, limit=_API_LIMIT)
        if await safety.get_card(card_id, session_id=session.id) is None:
            raise APIError(404, "not_found", "会话中不存在该卡片")
        try:
            approval = await safety.record_card_decision(
                card_id,
                session_id=session.id,
                fingerprint=body.fingerprint,
                decision=body.decision,
                actor="web",
                reason=project_text(body.reason),
            )
        except ActionError as exc:
            status = 404 if exc.code == "not_found" else 409
            raise APIError(status, exc.code, exc.message) from None
        return JSONResponse({"decision_id": approval.id, "status": approval.decision.value})

    async def monitoring_snapshot(_request: Request) -> JSONResponse:
        snapshot = await monitoring() if monitoring is not None else _empty_monitoring_snapshot()
        alert_block = snapshot["alerts"]
        alert_data = alert_block["data"]
        if alert_data is not None:
            groups = alert_data["groups"]
            open_sessions = await sessions.list_open_alert_sessions(
                [group["group_key"] for group in groups if group["group_key"]]
            )
            pending = await safety.pending_card_sessions([row.id for row in open_sessions])
            by_key = {
                row.group_key: {"session_id": row.id, "pending_approval": row.id in pending}
                for row in open_sessions
            }
            alerts = [{**group, "session": by_key.get(group["group_key"])} for group in groups]
            snapshot = {
                **snapshot,
                "alerts": {**alert_block, "data": {**alert_data, "groups": alerts}},
            }
        return JSONResponse(project_payload(snapshot))

    static_routes = (
        [Mount(STATIC_PREFIX.rstrip("/"), app=StaticFiles(directory=STATIC_DIR))]
        if STATIC_DIR.is_dir()
        else []
    )
    app = Starlette(
        routes=[
            Route("/", page),
            Route("/sessions/{session_id}", page),
            *static_routes,
            Route("/login", login_page, methods=["GET"]),
            Route("/login", login, methods=["POST"]),
            Route("/api/sessions", list_sessions),
            Route("/api/sessions", create_session, methods=["POST"]),
            Route("/api/sessions/{session_id}", session_snapshot),
            Route("/api/sessions/{session_id}/events", list_events),
            Route("/api/sessions/{session_id}/messages", send_message, methods=["POST"]),
            Route("/api/sessions/{session_id}/resume", resume, methods=["POST"]),
            Route("/api/sessions/{session_id}/close", close, methods=["POST"]),
            Route("/api/sessions/{session_id}/cards/{card_id}/decision", decide, methods=["POST"]),
            Route("/api/monitoring", monitoring_snapshot),
        ],
        exception_handlers={HTTPException: http_error},
        middleware=[Middleware(_LocalBrowser, settings=settings)],
    )
    app.router.redirect_slashes = False
    return app
