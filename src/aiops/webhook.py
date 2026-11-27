"""实验网关的 AlertManager 接收端，按告警组持久化会话。"""

import hmac
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, Field, StringConstraints, field_validator
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from aiops.actions.remediation_alerts import (
    clearance_state,
    link_firing,
    lock_workload_alerts,
    record_firing,
    record_resolved,
)
from aiops.actions.safety import ActionSafety
from aiops.config import Settings
from aiops.http_api import APIError, http_error, read_json
from aiops.sessions.projection import project_text
from aiops.sessions.store import SessionStore

ALERT_LABELS = (
    "severity",
    "namespace",
    "service",
    "deployment",
    "pod",
    "container",
    "job",
    "instance",
    "cluster",
)
AlertStatus = Literal["firing", "resolved"]


class Alert(BaseModel):
    status: AlertStatus
    labels: dict[str, str]
    annotations: dict[str, str] | None = None
    fingerprint: Annotated[str, StringConstraints(pattern=r"^[a-fA-F0-9]{16}$")] | None = None
    starts_at: AwareDatetime | None = Field(default=None, alias="startsAt")

    def identity(self) -> tuple[str, AwareDatetime] | None:
        if self.fingerprint is not None and self.starts_at is not None:
            return self.fingerprint.lower(), self.starts_at
        return None

    @field_validator("labels", "annotations")
    @classmethod
    def validate_values(cls, values: dict[str, str] | None) -> dict[str, str] | None:
        if values and any("\0" in value for value in values.values()):
            raise ValueError("告警内容含无效字符")
        return values

    @property
    def name(self) -> str:
        return self.labels.get("alertname") or "unknown"

    def describe(self) -> str:
        labels = " ".join(f"{key}={self.labels[key]}" for key in ALERT_LABELS if key in self.labels)
        annotations = self.annotations or {}
        summary = annotations.get("summary") or annotations.get("description") or ""
        return " ".join(part for part in (self.name, labels, summary) if part)


class AlertGroup(BaseModel):
    group_key: Annotated[str, StringConstraints(min_length=1, max_length=1024)] = Field(
        alias="groupKey"
    )
    status: AlertStatus
    alerts: list[Alert]

    @field_validator("group_key")
    @classmethod
    def validate_group_key(cls, value: str) -> str:
        if not value.strip() or len(value.encode()) > 1024 or "\0" in value:
            raise ValueError("告警组键无效")
        return value


def _authorised(request: Request, expected: bytes) -> bool:
    values = request.headers.getlist("authorization")
    if len(values) != 1 or "x-vigil-token" in request.headers:
        return False
    scheme, _, presented = values[0].partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(presented.encode("latin-1"), expected)


def build_app(
    settings: Settings,
    *,
    sessions: SessionStore,
    safety: ActionSafety,
    remediation_available: bool = False,
) -> Starlette:
    settings.validate_webhook_listener()
    if settings.webhook_token is None:
        raise ValueError("告警监听需要配置令牌")
    expected = settings.webhook_token.get_secret_value().encode("ascii")

    async def alertmanager_webhook(request: Request) -> JSONResponse:
        if not _authorised(request, expected):
            raise APIError(401, "unauthorized", "告警凭证无效")
        group = await read_json(request, AlertGroup, limit=1024 * 1024)
        firing = [alert for alert in group.alerts if alert.status == "firing"]
        resolved = [alert for alert in group.alerts if alert.status == "resolved"]
        session_id = None
        outcome = "ignored"
        async with sessions.alert_transaction(group.group_key) as conn:
            scope = {"cluster": settings.cluster, "group_key": group.group_key}
            keys = {
                (alert.labels["namespace"], alert.labels["target"])
                for alert in group.alerts
                if alert.identity() and {"namespace", "target"} <= alert.labels.keys()
            }
            for namespace, target in sorted(keys):
                await lock_workload_alerts(conn, settings.cluster, namespace, target)
            for alert in resolved:
                if identity := alert.identity():
                    await record_resolved(
                        conn,
                        **scope,
                        fingerprint=identity[0],
                        starts_at=identity[1],
                        labels=alert.labels,
                    )

            fresh = []
            if group.status == "firing":
                for alert in firing:
                    identity = alert.identity()
                    if resolved and identity is None:
                        # 旧告警的混合批次只通报恢复，避免重复追加 firing。
                        scoped = await sessions.get_open_alert_session(conn, group.group_key)
                        if scoped is not None:
                            continue
                    if identity is None or await record_firing(
                        conn,
                        **scope,
                        fingerprint=identity[0],
                        starts_at=identity[1],
                        labels=alert.labels,
                    ):
                        fresh.append(alert)

            if fresh:
                names = tuple(dict.fromkeys(alert.name for alert in fresh))
                text = "请排查告警：\n" + "\n".join(alert.describe() for alert in fresh)
                receipt = await sessions.record_alert(
                    conn,
                    group.group_key,
                    title=project_text("、".join(names)),
                    text=text,
                    display_text=project_text(text),
                    repeat_display_text=project_text(f"告警再次触发：{'、'.join(names)}"),
                    alert_names=names,
                )
                session_id = receipt.session_id
                outcome = "created" if receipt.created else "repeated"
                for alert in fresh:
                    if identity := alert.identity():
                        await link_firing(
                            conn,
                            **scope,
                            fingerprint=identity[0],
                            starts_at=identity[1],
                            session_id=session_id,
                            evaluate=remediation_available and settings.remediation_auto_enabled,
                        )
            elif resolved or group.status == "resolved" or firing:
                session = await sessions.get_open_alert_session(conn, group.group_key)
                if session is not None:
                    session_id = session.id
                    if resolved or group.status == "resolved":
                        known, active, blocked = await clearance_state(conn, session.id)
                        matched = bool(resolved) and all(alert.identity() for alert in resolved)
                        if group.status == "resolved" and not active and not blocked:
                            if not known or matched:
                                await safety.close_session(
                                    conn, session.id, reason="alert_resolved"
                                )
                                outcome = "closed"
                        if outcome != "closed" and resolved:
                            names = "、".join(dict.fromkeys(alert.name for alert in resolved))
                            await sessions.append_alert_message(
                                conn,
                                session.id,
                                display_text=project_text(f"部分告警已恢复：{names}"),
                            )
                            outcome = "partially_resolved"
                    else:
                        outcome = "repeated"
        return JSONResponse({"session_id": session_id, "outcome": outcome})

    app = Starlette(
        routes=[Route("/webhook/alertmanager", alertmanager_webhook, methods=["POST"])],
        exception_handlers={HTTPException: http_error},
    )
    app.router.redirect_slashes = False
    return app
