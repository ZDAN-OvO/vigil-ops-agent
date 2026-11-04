"""CLI 入口；W0 仅提供不访问外部服务的实现状态查询。"""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated

import typer

from aiops import __version__
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
from aiops.investigation.factory import open_runtime
from aiops.investigation.runtime import Report
from aiops.webhook import build_app

app = typer.Typer(
    help="Vigil AIOps：故障调查与受控修复（当前支持只读调查）。",
    no_args_is_help=True,
    add_completion=False,
)
db_app = typer.Typer(help="实验 PostgreSQL 的迁移与连接检查。", no_args_is_help=True)
app.add_typer(db_app, name="db")


class OutputFormat(StrEnum):
    TEXT = "text"
    JSON = "json"


def _require_dsn() -> str:
    dsn = Settings().database_url
    if not dsn:
        typer.echo("未配置 VIGIL_DATABASE_URL，无法连接实验 PostgreSQL。", err=True)
        raise typer.Exit(code=2)
    return dsn


@db_app.command("migrate")
def db_migrate(output: OutputFormat = OutputFormat.TEXT) -> None:
    """应用尚未执行的业务表迁移；可重复执行。"""

    async def run() -> list[str]:
        async with Store.open(_require_dsn()) as store:
            return await store.migrate()

    try:
        applied = asyncio.run(run())
    except StoreUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        typer.echo(json.dumps({"schema_version": "1", "applied": applied}, ensure_ascii=False))
    else:
        typer.echo("已应用：" + (", ".join(applied) if applied else "无（已是最新）"))


@db_app.command("status")
def db_status(output: OutputFormat = OutputFormat.TEXT) -> None:
    """列出已应用的迁移版本，确认连接可用。"""

    async def run() -> list[str]:
        async with Store.open(_require_dsn()) as store:
            return await store.applied_migrations()

    try:
        applied = asyncio.run(run())
    except StoreUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        typer.echo(json.dumps({"schema_version": "1", "applied": applied}, ensure_ascii=False))
    else:
        typer.echo("已应用迁移：" + (", ".join(applied) if applied else "无"))


@app.callback()
def main() -> None:
    """业务命令按阶段接入，帮助输出不初始化数据库或集群。"""


@app.command()
def status(output: OutputFormat = OutputFormat.TEXT) -> None:
    """查看当前实现范围；不是集群健康检查。"""
    payload = {
        "schema_version": "1",
        "version": __version__,
        "stage": "W0",
        "capabilities": {"cli": True, "investigation": True, "remediation": False},
        "external_services_checked": False,
    }
    if output == OutputFormat.JSON:
        typer.echo(json.dumps(payload, ensure_ascii=False))
    else:
        typer.echo(f"Vigil AIOps {__version__} · W0：只读调查可用，受控处置尚未实现。")


def _print_report(report: Report, output: OutputFormat) -> None:
    if output == OutputFormat.JSON:
        payload = {
            "schema_version": "1",
            "status": report.status.value,
            "investigation_id": report.investigation_id,
            "data": {
                "conclusion": report.conclusion,
                "termination_reason": report.termination_reason,
                "hypotheses": report.hypotheses,
                "evidence_count": report.evidence_count,
                "tool_call_count": report.tool_call_count,
            },
        }
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    typer.echo(f"调查 {report.investigation_id}：{report.status.value}")
    if report.conclusion:
        typer.echo(f"根因候选：{report.conclusion}")
    if report.termination_reason:
        typer.echo(f"终止原因：{report.termination_reason}")
    typer.echo(f"证据 {report.evidence_count} 条，工具调用 {report.tool_call_count} 次")
    for hypothesis in report.hypotheses:
        typer.echo(f"  - [{hypothesis['status']}] {hypothesis['statement']}")


@app.command()
def investigate(
    service: str = typer.Option(..., help="业务服务名"),
    symptoms: str = typer.Option(..., help="观察到的现象"),
    namespace: str = typer.Option("vigil-shop", help="业务命名空间"),
    cluster: str = typer.Option("vigil-lab", help="集群名，仅用于记录"),
    lookback_minutes: int = typer.Option(15, min=1, max=180, help="取证时间窗"),
    context: Annotated[list[str] | None, typer.Option(help="人工补充的上下文，可重复")] = None,
    output: OutputFormat = OutputFormat.TEXT,
) -> None:
    """运行一次只读调查；不执行任何集群写操作。"""
    settings = Settings()
    if not settings.model_name:
        typer.echo("未配置 VIGIL_MODEL_NAME，无法调用模型。", err=True)
        raise typer.Exit(code=2)
    dsn = _require_dsn()

    now = datetime.now(UTC)
    incident = Incident(
        id=f"inc-{uuid.uuid4().hex[:12]}",
        environment=settings.environment,
        cluster=cluster,
        namespace=namespace,
        service=service,
        trigger="manual",
        symptoms=symptoms,
        time_range=TimeRange(start=now - timedelta(minutes=lookback_minutes), end=now),
        context=context or [],
    )
    investigation = Investigation(
        id=f"inv-{uuid.uuid4().hex[:12]}",
        incident_id=incident.id,
        status=InvestigationStatus.RUNNING,
        model=settings.model_name,
        policy_version="mvp-1",
        budget=Budget(
            max_tool_calls=settings.investigation_budget_tool_calls,
            max_model_calls=settings.investigation_budget_model_calls,
            max_wall_clock_seconds=settings.investigation_budget_seconds,
        ),
        started_at=now,
    )

    async def run() -> Report:
        async with Store.open(dsn) as store:
            await store.migrate()
            await store.save_incident(incident)
            await store.create_investigation(investigation)
            await store.append_audit(
                [
                    AuditEvent(
                        id=f"audit-{uuid.uuid4().hex[:12]}",
                        correlation_id=investigation.id,
                        actor="cli",
                        event="investigation.started",
                        occurred_at=now,
                        payload={"service": service, "model": settings.model_name},
                    )
                ]
            )
            async with open_runtime(
                settings=settings,
                store=store,
                dsn=dsn,
                incident=incident,
                investigation=investigation,
            ) as runtime:
                return await runtime.run()

    try:
        report = asyncio.run(run())
    except StoreUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    _print_report(report, output)
    if report.status is InvestigationStatus.COMPLETED:
        raise typer.Exit(code=0)
    if report.status is InvestigationStatus.INCONCLUSIVE:
        raise typer.Exit(code=5)
    raise typer.Exit(code=1)


@app.command()
def incidents(
    limit: int = typer.Option(20, min=1, max=200),
    output: OutputFormat = OutputFormat.TEXT,
) -> None:
    """列出最近的告警事件及关联调查。"""

    async def run() -> list[dict]:
        async with Store.open(_require_dsn()) as store:
            items = await store.list_incidents(limit)
            return [
                {
                    "incident_id": incident.id,
                    "service": incident.service,
                    "symptoms": incident.symptoms,
                    "trigger": incident.trigger,
                    "alert_ids": incident.alert_ids,
                    "investigations": [
                        {"id": investigation.id, "status": investigation.status.value}
                        for investigation in await store.list_investigations(incident.id)
                    ],
                }
                for incident in items
            ]

    try:
        items = asyncio.run(run())
    except StoreUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        typer.echo(json.dumps({"schema_version": "1", "data": items}, ensure_ascii=False))
        return
    for item in items:
        ids = ", ".join(f"{inv['id']}({inv['status']})" for inv in item["investigations"])
        typer.echo(
            f"{item['incident_id']} {item['service']} [{item['trigger']}] {item['symptoms']}"
        )
        typer.echo(f"  调查：{ids or '无'}")


@app.command()
def report(investigation_id: str, output: OutputFormat = OutputFormat.TEXT) -> None:
    """按调查 ID 查看已持久化的结论与证据统计。"""

    async def run() -> Report:
        async with Store.open(_require_dsn()) as store:
            saved = await store.get_investigation(investigation_id)
            if saved is None:
                raise LookupError(investigation_id)
            hypotheses = await store.list_hypotheses(investigation_id)
            evidence = await store.list_evidence(investigation_id)
            calls = await store.list_tool_calls(investigation_id)
        return Report(
            investigation_id=saved.id,
            status=saved.status,
            conclusion=saved.conclusion,
            termination_reason=saved.termination_reason,
            hypotheses=[
                {"statement": hypothesis.statement, "status": hypothesis.status.value}
                for hypothesis in hypotheses
            ],
            evidence_count=len(evidence),
            tool_call_count=len(calls),
        )

    try:
        result = asyncio.run(run())
    except StoreUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except LookupError as exc:
        typer.echo(f"调查不存在：{exc}", err=True)
        raise typer.Exit(code=2) from exc
    _print_report(result, output)


@app.command()
def serve(
    host: str | None = typer.Option(None, help="监听地址，默认读配置"),
    port: int | None = typer.Option(None, help="监听端口，默认读配置"),
) -> None:
    """启动 AlertManager webhook 服务；收到告警后异步发起调查。"""
    settings = Settings()
    if settings.webhook_token is None:
        typer.echo("未配置 VIGIL_WEBHOOK_TOKEN，拒绝启动无鉴权的 webhook。", err=True)
        raise typer.Exit(code=2)
    _require_dsn()
    if settings.model_name is None:
        typer.echo("未配置 VIGIL_MODEL_NAME，无法发起调查。", err=True)
        raise typer.Exit(code=2)

    import uvicorn

    uvicorn.run(
        build_app(settings),
        host=host or settings.webhook_host,
        port=port or settings.webhook_port,
    )


if __name__ == "__main__":
    app()
