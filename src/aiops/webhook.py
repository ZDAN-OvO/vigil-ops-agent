"""AlertManager webhook：告警转事件与调查，按指纹去重，并立刻返回。

真值隔离从入口开始：只保留白名单标签，场景名、脚本路径等字段不进入事件。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from aiops.config import Settings
from aiops.contracts import (
    AuditEvent,
    Budget,
    Incident,
    Investigation,
    InvestigationStatus,
    TimeRange,
)
from aiops.db import Store, StoreUnavailable
from aiops.investigation.factory import open_agent

TOKEN_HEADER = "x-vigil-token"

# 只保留调查需要的标签；scenario 等实验标记一律丢弃。
ALLOWED_LABELS = frozenset(
    {
        "alertname",
        "severity",
        "namespace",
        "service",
        "deployment",
        "pod",
        "container",
        "job",
        "instance",
        "cluster",
    }
)

Runner = Callable[[Store, Settings, Incident, Investigation], Awaitable[None]]


@dataclass(frozen=True)
class AlertEvent:
    fingerprint: str
    alertname: str
    severity: str
    namespace: str
    service: str
    symptoms: str
    starts_at: datetime
    labels: dict[str, str]


def _parse_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.year < 2000:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _fingerprint(labels: dict[str, str]) -> str:
    canonical = ",".join(f"{key}={labels[key]}" for key in sorted(labels))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def map_firing_alerts(payload: dict[str, Any]) -> list[AlertEvent]:
    events = []
    for alert in payload.get("alerts") or []:
        if not isinstance(alert, dict) or alert.get("status") != "firing":
            continue
        labels = {str(k): str(v) for k, v in (alert.get("labels") or {}).items()}
        annotations = alert.get("annotations") or {}
        starts_at = _parse_time(alert.get("startsAt")) or datetime.now(UTC)
        alertname = labels.get("alertname", "unknown")
        events.append(
            AlertEvent(
                fingerprint=str(alert.get("fingerprint") or _fingerprint(labels)),
                alertname=alertname,
                severity=labels.get("severity", "unknown"),
                namespace=labels.get("namespace", ""),
                service=labels.get("service") or labels.get("job") or alertname,
                symptoms=str(
                    annotations.get("summary") or annotations.get("description") or alertname
                ),
                starts_at=starts_at,
                labels={k: v for k, v in labels.items() if k in ALLOWED_LABELS},
            )
        )
    return events


def build_incident(events: list[AlertEvent], settings: Settings, *, now: datetime) -> Incident:
    first = events[0]
    start = min(event.starts_at for event in events)
    context = [
        f"alertname={first.alertname}",
        f"severity={first.severity}",
        *(
            f"{key}={value}"
            for key, value in sorted(first.labels.items())
            if key not in {"alertname", "severity"}
        ),
    ]
    return Incident(
        id=f"inc-{uuid.uuid4().hex[:12]}",
        environment=settings.environment,
        cluster=first.labels.get("cluster") or settings.cluster,
        namespace=first.labels.get("namespace") or settings.namespace,
        service=first.service,
        trigger="alert",
        symptoms="；".join(dict.fromkeys(event.symptoms for event in events)),
        time_range=TimeRange(start=start, end=max(now, start + timedelta(minutes=1))),
        alert_ids=list(dict.fromkeys(event.fingerprint for event in events)),
        context=context,
    )


def build_investigation(incident: Incident, settings: Settings, *, now: datetime) -> Investigation:
    if settings.model_name is None:
        raise ValueError("未配置 VIGIL_MODEL_NAME")
    return Investigation(
        id=f"inv-{uuid.uuid4().hex[:12]}",
        incident_id=incident.id,
        status=InvestigationStatus.PENDING,
        model=settings.model_name,
        policy_version="mvp-1",
        budget=Budget(
            max_tool_calls=settings.investigation_budget_tool_calls,
            max_model_calls=settings.investigation_budget_model_calls,
            max_wall_clock_seconds=settings.investigation_budget_seconds,
            max_action_seconds=settings.investigation_budget_action_seconds,
        ),
        started_at=now,
    )


async def default_runner(
    store: Store, settings: Settings, incident: Incident, investigation: Investigation
) -> None:
    assert settings.database_url is not None
    await store.mark_investigation_running(investigation.id, datetime.now(UTC))
    try:
        async with open_agent(
            settings=settings,
            store=store,
            dsn=settings.database_url,
            incident=incident,
            investigation=investigation,
        ) as runtime:
            await runtime.run()
    except Exception as exc:  # noqa: BLE001 - 后台任务失败必须落状态，不能静默
        await store.set_investigation_status(
            investigation.id,
            InvestigationStatus.ABORTED,
            termination_reason=f"运行时异常：{exc.__class__.__name__}",
            finished_at=datetime.now(UTC),
        )


def _presented_token(request: Request) -> str:
    header = request.headers.get(TOKEN_HEADER)
    if header:
        return header
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def _authorised(request: Request) -> bool:
    expected = request.app.state.settings.webhook_token
    if expected is None:
        return False
    return hmac.compare_digest(_presented_token(request), expected.get_secret_value())


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


async def alertmanager_webhook(request: Request) -> JSONResponse:
    if not _authorised(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "invalid payload"}, status_code=400)

    events = map_firing_alerts(payload)
    if not events:
        return JSONResponse({"status": "ignored", "reason": "no firing alerts"})

    settings: Settings = request.app.state.settings
    store: Store = request.app.state.store
    now = datetime.now(UTC)
    fingerprints = list(dict.fromkeys(event.fingerprint for event in events))

    existing = await store.find_recent_incident(
        fingerprints, since=now - timedelta(minutes=settings.dedup_minutes)
    )
    if existing is not None:
        return JSONResponse(
            {"status": "deduplicated", "incident_id": existing.id},
            status_code=200,
        )

    incident = build_incident(events, settings, now=now)
    investigation = build_investigation(incident, settings, now=now)
    await store.save_incident(incident)
    await store.create_investigation(investigation)
    await store.append_audit(
        [
            AuditEvent(
                id=f"audit-{uuid.uuid4().hex[:12]}",
                correlation_id=investigation.id,
                actor="alertmanager",
                event="alert.received",
                occurred_at=now,
                payload={
                    "incident_id": incident.id,
                    "alerts": [event.alertname for event in events],
                    "fingerprints": fingerprints,
                },
            )
        ]
    )

    task = asyncio.create_task(request.app.state.runner(store, settings, incident, investigation))
    request.app.state.tasks.add(task)
    task.add_done_callback(request.app.state.tasks.discard)

    return JSONResponse(
        {
            "status": "accepted",
            "incident_id": incident.id,
            "investigation_id": investigation.id,
        },
        status_code=202,
    )


def build_app(settings: Settings, *, runner: Runner | None = None) -> Starlette:
    @asynccontextmanager
    async def lifespan(app: Starlette):
        if settings.database_url is None:
            raise StoreUnavailable("未配置 VIGIL_DATABASE_URL")
        async with Store.open(settings.database_url) as store:
            await store.migrate()
            app.state.store = store
            app.state.tasks = set()
            try:
                yield
            finally:
                for task in list(app.state.tasks):
                    task.cancel()

    app = Starlette(
        routes=[
            Route("/health", health),
            Route("/webhook/alertmanager", alertmanager_webhook, methods=["POST"]),
        ],
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.runner = runner or default_runner
    return app
