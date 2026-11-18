"""本机页面与 JSON/SSE 接口。告警路由由独立应用提供。"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from psycopg_pool import PoolTimeout
from starlette.exceptions import HTTPException

from aiops.config import PROJECT_ROOT, Settings
from aiops.contracts import Incident, TimeRange
from aiops.db import ConversationConflict, Store, StoreUnavailable
from aiops.doctor import kube_configuration
from aiops.web.lifecycle import decide_proposal
from aiops.web.models import (
    CreateRequest,
    DecisionRequest,
    ErrorResponse,
    MessageRequest,
    ServicesResponse,
    Snapshot,
    WriteResponse,
)
from aiops.web.snapshot import read_snapshot
from aiops.webhook import build_investigation

ERRORS = {
    "host_rejected": (421, "请通过本机 Vigil 地址访问。"),
    "origin_rejected": (403, "请从 Vigil 页面提交操作。"),
    "invalid_request": (422, "请求格式有误，请检查输入。"),
    "not_found": (404, "没有找到此资源。"),
    "conversation_active": (409, "已有调查正在进行。"),
    "conversation_finished": (409, "这次调查已经结束。"),
    "idempotency_conflict": (409, "此请求编号已用于另一项操作。"),
    "not_waiting": (409, "调查当前未在等待此提案。"),
    "not_pending": (409, "这份提案已经有决定。"),
    "hash_mismatch": (409, "提案已更新，请刷新后重新确认。"),
    "store_unavailable": (503, "数据库暂时不可用。"),
    "cluster_unavailable": (503, "暂时无法获取服务列表。"),
    "method_not_allowed": (405, "此资源不接受该请求方式。"),
    "internal_error": (500, "请求暂时无法处理。"),
}
SECURITY_HEADERS = {
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}


def error(code: str) -> JSONResponse:
    status, message = ERRORS[code]
    return JSONResponse(
        ErrorResponse(code=code, message=message).model_dump(),
        status_code=status,
        headers=SECURITY_HEADERS,
    )


def list_services(settings: Settings) -> list[str]:
    from kubernetes import client

    with client.ApiClient(kube_configuration(settings)) as api:
        response = client.CoreV1Api(api).list_namespaced_service(
            settings.namespace,
            _request_timeout=(settings.tool_timeout_seconds,) * 2,
        )
    return sorted(item.metadata.name for item in response.items)


def build_web_app(settings: Settings, store: Store | None, *, dist: Path | None = None) -> FastAPI:
    app = FastAPI(
        title="Vigil Web API",
        version="1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        responses={
            403: {"model": ErrorResponse},
            421: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            500: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    dist = dist if dist is not None else PROJECT_ROOT / "web" / "dist"
    hosts = {f"{host}:{settings.web_port}" for host in ("localhost", "127.0.0.1", "[::1]")}
    origins = {f"http://{host}" for host in hosts}
    if settings.web_dev_origin:
        origins.add(settings.web_dev_origin)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        presented_hosts = request.headers.getlist("host")
        if len(presented_hosts) != 1 or presented_hosts[0] not in hosts:
            return error("host_rejected")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            presented = request.headers.getlist("origin")
            if len(presented) != 1 or presented[0] not in origins:
                return error("origin_rejected")
        try:
            response = await call_next(request)
        except (StoreUnavailable, psycopg.OperationalError, PoolTimeout):
            response = error("store_unavailable")
        except Exception:
            response = error("internal_error")
        response.headers.update(SECURITY_HEADERS)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return error("invalid_request")

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error(
            {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "internal_error")
        )

    @app.exception_handler(ConversationConflict)
    async def conflict(request, exc):
        return error(exc.code)

    @app.get("/api/conversations/current", response_model=Snapshot)
    async def current():
        return await read_snapshot(store, settings)

    @app.post(
        "/api/conversations",
        status_code=201,
        response_model=WriteResponse,
        responses={200: {"model": WriteResponse}, 409: {"model": ErrorResponse}},
    )
    async def create(body: CreateRequest):
        now = datetime.now(UTC)
        incident = Incident(
            id=f"inc-{uuid4().hex[:12]}",
            environment=settings.environment,
            cluster=settings.cluster,
            namespace=settings.namespace,
            service=body.service,
            trigger="manual",
            symptoms=body.symptoms,
            time_range=TimeRange(start=now - timedelta(minutes=10), end=now),
        )
        investigation = build_investigation(incident, settings, now=now)
        result = await store.create_conversation(body.request_id, incident, investigation)
        return JSONResponse(
            WriteResponse(id=result.id).model_dump(), status_code=200 if result.replayed else 201
        )

    @app.post(
        "/api/conversations/{id}/messages",
        status_code=201,
        response_model=WriteResponse,
        responses={
            200: {"model": WriteResponse},
            404: {"model": ErrorResponse},
            409: {"model": ErrorResponse},
        },
    )
    async def append_message(id: str, body: MessageRequest):
        result = await store.append_conversation_message(body.request_id, id, body.content)
        return JSONResponse(
            WriteResponse(id=result.id).model_dump(), status_code=200 if result.replayed else 201
        )

    @app.post(
        "/api/proposals/{id}/decisions",
        status_code=202,
        response_model=WriteResponse,
        responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    )
    async def decision(id: str, body: DecisionRequest):
        result = await decide_proposal(
            store,
            id,
            request_id=body.request_id,
            decision=body.decision,
            reason=body.reason,
            submitted_hash=body.card_hash,
            actor=settings.web_operator,
            ttl=settings.action_approval_ttl_seconds,
        )
        return WriteResponse(id=result.id)

    @app.get("/api/services", response_model=ServicesResponse)
    async def services():
        try:
            items = await asyncio.to_thread(list_services, settings)
        except Exception:
            return error("cluster_unavailable")
        return ServicesResponse(services=items)

    @app.get(
        "/api/conversations/{id}/events",
        response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {}}}, 404: {"model": ErrorResponse}},
    )
    async def events(request: Request, id: str, after: int = 0):
        async with store._pool.connection() as conn:
            cur = await conn.execute("SELECT 1 FROM conversations WHERE id = %s", (id,))
            if not await cur.fetchone():
                return error("not_found")
        received = request.headers.get("last-event-id", "")
        cursor = max(0, after, int(received) if received.isdecimal() and len(received) <= 19 else 0)
        if cursor > 2**63 - 1:
            return error("invalid_request")

        async def stream():
            nonlocal cursor
            remaining = 0
            while not await request.is_disconnected():
                try:
                    records = await store.list_conversation_events(id, after=cursor)
                except (psycopg.OperationalError, PoolTimeout):
                    return
                for event in records:
                    cursor = event["id"]
                    yield f"id: {cursor}\ndata: change\n\n"
                if remaining <= 0 or records:
                    yield ": heartbeat\n\n"
                    remaining = 15
                await asyncio.sleep(1)
                remaining -= 1

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.api_route(
        "/api", methods=["GET", "HEAD", "POST", "PATCH", "PUT", "DELETE"], include_in_schema=False
    )
    @app.api_route(
        "/api/{path:path}",
        methods=["GET", "HEAD", "POST", "PATCH", "PUT", "DELETE"],
        include_in_schema=False,
    )
    async def missing_api(path: str = ""):
        return error("not_found")

    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def page(path: str):
        entry = dist / "index.html"
        if entry.is_file():
            return FileResponse(entry)
        return PlainTextResponse(
            "页面文件缺失。请在仓库根目录运行 make web-build。", status_code=503
        )

    return app
