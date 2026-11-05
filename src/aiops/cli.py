"""CLI 入口；W0 仅提供不访问外部服务的实现状态查询。"""

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from aiops import __version__
from aiops.actions.executor import ActionExecutor
from aiops.actions.restart_pod import PodRestartAdapter
from aiops.actions.rollout_undo import ActionError, RolloutUndoAdapter
from aiops.config import Settings
from aiops.contracts import (
    AuditEvent,
    Budget,
    Incident,
    Investigation,
    InvestigationStatus,
    ProposalStatus,
    TimeRange,
)
from aiops.db import Store, StoreUnavailable
from aiops.investigation.explain import ExplainError, ExplainService
from aiops.investigation.factory import open_runtime
from aiops.investigation.runtime import Report
from aiops.knowledge import load_runbooks
from aiops.model import ModelClient
from aiops.tools.prometheus import PrometheusClient
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
        "capabilities": {"cli": True, "investigation": True, "remediation": True},
        "external_services_checked": False,
    }
    if output == OutputFormat.JSON:
        typer.echo(json.dumps(payload, ensure_ascii=False))
    else:
        typer.echo(f"Vigil AIOps {__version__} · W0：只读调查与受控回退可用，自动处置尚未实现。")


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
                "proposal_id": report.proposal_id,
            },
        }
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    typer.echo(f"调查 {report.investigation_id}：{report.status.value}")
    if report.proposal_id:
        typer.echo(f"动作提案：{report.proposal_id}")
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


@asynccontextmanager
async def _open_executor(settings: Settings, store: Store):
    async with PrometheusClient.connect(
        settings.prometheus_url, timeout_seconds=settings.tool_timeout_seconds
    ) as prometheus:
        yield ActionExecutor(
            store=store,
            adapter=RolloutUndoAdapter(
                kubeconfig=settings.kubeconfig, context_name=settings.kube_context
            ),
            restart_adapter=PodRestartAdapter(
                kubeconfig=settings.kubeconfig, context_name=settings.kube_context
            ),
            prometheus=prometheus,
            runtime_path=settings.runtime_path,
            cluster=settings.cluster,
            namespace=settings.namespace,
            rollout_timeout_seconds=settings.action_rollout_timeout_seconds,
            verify_seconds=settings.action_verify_seconds,
            sample_seconds=settings.action_sample_seconds,
            approval_ttl_seconds=settings.action_approval_ttl_seconds,
        )


def _report_attempt(attempt, output: OutputFormat) -> None:
    if output == OutputFormat.JSON:
        typer.echo(
            json.dumps(
                {
                    "schema_version": "1",
                    "status": attempt.status.value,
                    "action_id": attempt.proposal_id,
                    "data": {"result": attempt.result, "verification": attempt.verification},
                },
                ensure_ascii=False,
            )
        )
        return
    typer.echo(f"执行 {attempt.proposal_id}：{attempt.status.value}")
    typer.echo(f"结果：{json.dumps(attempt.result, ensure_ascii=False)}")
    typer.echo(f"验证：{json.dumps(attempt.verification, ensure_ascii=False)}")


def _duration(since: datetime) -> str:
    seconds = max(0, int((datetime.now(UTC) - since).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


@app.command()
def explain(
    investigation_id: str = typer.Argument(..., help="已完成调查的 ID"),
    question: str = typer.Argument(..., help="追问内容"),
    output: OutputFormat = OutputFormat.TEXT,
) -> None:
    """只读追问已有调查：只用已存记录回答，不新增查询，也不执行动作。"""
    settings = Settings()
    if not settings.model_name:
        typer.echo("未配置 VIGIL_MODEL_NAME，无法调用模型。", err=True)
        raise typer.Exit(code=2)

    async def run():
        async with Store.open(_require_dsn()) as store:
            async with ModelClient.connect(
                settings.model_base_url,
                model=settings.model_name,
                api_key=settings.model_api_key,
                timeout_seconds=settings.model_timeout_seconds,
                max_retries=settings.model_max_retries,
            ) as model:
                return await ExplainService(store=store, model=model).answer(
                    investigation_id, question
                )

    try:
        result = asyncio.run(run())
    except ExplainError as exc:
        typer.echo(f"无法回答（{exc.code}）：{exc.message}", err=True)
        raise typer.Exit(code=1) from exc
    except StoreUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    if output == OutputFormat.JSON:
        typer.echo(
            json.dumps(
                {
                    "schema_version": "1",
                    "status": "answered",
                    "investigation_id": result.investigation_id,
                    "data": {
                        "answer": result.answer,
                        "citations": result.citations,
                        "missing": result.missing,
                        "needs_new_query": result.needs_new_query,
                    },
                },
                ensure_ascii=False,
            )
        )
        return
    typer.echo(result.answer)
    typer.echo(f"引用：{', '.join(result.citations) if result.citations else '无'}")
    if result.missing:
        typer.echo(f"缺少：{'；'.join(result.missing)}")
    if result.needs_new_query:
        typer.echo("需要新的取证才能回答；本次没有发出任何查询或动作。")


@app.command("knowledge")
def knowledge(
    action: str = typer.Argument("list", help="list 或 import"),
    path: str = typer.Option("runbooks", help="import 时的 JSON 文件或目录"),
) -> None:
    """登记与查看结构化 runbook；导入只写知识表，不改集群。"""

    async def run():
        async with Store.open(_require_dsn()) as store:
            await store.migrate()
            if action == "import":
                target = Path(path)
                files = sorted(target.glob("*.json")) if target.is_dir() else [target]
                # 先解析全部文件再写库；有坏文件时一条都不写，避免半套知识生效。
                parsed = []
                for file in files:
                    try:
                        parsed.extend(load_runbooks(file))
                    except (OSError, ValueError) as exc:
                        raise ActionError("invalid_runbook", f"{file}：{exc}") from exc
                for runbook in parsed:
                    await store.upsert_runbook(runbook)
                return len(parsed), []
            return 0, await store.list_runbooks(limit=50)

    try:
        count, items = asyncio.run(run())
    except ActionError as exc:
        typer.echo(f"导入失败（{exc.code}）：{exc.message}", err=True)
        raise typer.Exit(code=1) from exc
    except StoreUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    if action == "import":
        typer.echo(f"已导入 {count} 条 runbook")
        return
    for item in items:
        typer.echo(f"{item.id} v{item.version} {item.title}｜{item.summary}")


@app.command("act")
def act(
    kind: str = typer.Option("rollout-undo", help="动作类型：rollout-undo 或 restart-pod"),
    deployment: str | None = typer.Option(None, help="目标 Deployment，rollout-undo 必填"),
    pod: str | None = typer.Option(None, help="目标 Pod，restart-pod 必填"),
    service: str | None = typer.Option(None, help="验证恢复用的 Service，restart-pod 必填"),
    path: str = typer.Option("/", help="验证请求的路径"),
    reason: str = typer.Option(..., help="发起原因"),
    namespace: str | None = typer.Option(None, help="默认读配置"),
    investigation: str | None = typer.Option(None, help="关联的调查 ID"),
    actor: str = typer.Option("operator", help="发起人"),
    output: OutputFormat = OutputFormat.TEXT,
) -> None:
    """提交一个受控动作；支持 rollout-undo 与 restart-pod。只提案，不执行。"""

    async def run():
        async with Store.open(_require_dsn()) as store:
            await store.migrate()
            async with _open_executor(Settings(), store) as executor:
                if kind == "restart-pod":
                    if not pod or not service:
                        raise ActionError("invalid_argument", "restart-pod 需要 --pod 与 --service")
                    return await executor.propose_restart_pod(
                        pod=pod,
                        service=service,
                        path=path,
                        reason=reason,
                        actor=actor,
                        namespace=namespace,
                        investigation_id=investigation,
                    )
                if kind != "rollout-undo":
                    raise ActionError("invalid_argument", f"未知动作类型：{kind}")
                if not deployment:
                    raise ActionError("invalid_argument", "rollout-undo 需要 --deployment")
                return await executor.propose_rollback(
                    deployment=deployment,
                    reason=reason,
                    actor=actor,
                    namespace=namespace,
                    investigation_id=investigation,
                )

    try:
        proposal, decision = asyncio.run(run())
    except ActionError as exc:
        typer.echo(f"无法提案（{exc.code}）：{exc.message}", err=True)
        raise typer.Exit(code=1) from exc
    except StoreUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    if output == OutputFormat.JSON:
        typer.echo(
            json.dumps(
                {
                    "schema_version": "1",
                    "status": proposal.status.value,
                    "action_id": proposal.id,
                    "data": {
                        "target": proposal.target.model_dump(),
                        "parameters": proposal.parameters,
                        "disposition": decision.disposition.value,
                        "reasons": decision.reasons,
                    },
                },
                ensure_ascii=False,
            )
        )
    else:
        typer.echo(f"提案 {proposal.id}：{proposal.status.value}")
        if proposal.kind.value == "rollout_undo":
            detail = (
                f"revision {proposal.parameters['from_revision']} → "
                f"{proposal.parameters['to_revision']}"
            )
        else:
            detail = f"{proposal.parameters['pod']}（{proposal.parameters['replica_set']}）"
        typer.echo(
            f"目标 {proposal.target.kind} "
            f"{proposal.target.namespace}/{proposal.target.name}：{detail}"
        )
        typer.echo(f"风险判定：{decision.disposition.value}")
        for item in decision.reasons:
            typer.echo(f"  - {item}")
    raise typer.Exit(code=4 if proposal.status.value == "denied" else 3)


@app.command()
def approvals(
    status: str = typer.Option("pending_approval", help="按状态过滤"),
    limit: int = typer.Option(20, min=1, max=200),
    output: OutputFormat = OutputFormat.TEXT,
) -> None:
    """列出动作提案，默认只看待审批的。"""
    try:
        wanted = ProposalStatus(status)
    except ValueError as exc:
        typer.echo(f"未知状态：{status}", err=True)
        raise typer.Exit(code=2) from exc

    async def run():
        async with Store.open(_require_dsn()) as store:
            return await store.list_proposals(status=wanted, limit=limit)

    try:
        items = asyncio.run(run())
    except StoreUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    if output == OutputFormat.JSON:
        typer.echo(
            json.dumps(
                {
                    "schema_version": "1",
                    "data": [
                        {
                            "action_id": item.id,
                            "kind": item.kind.value,
                            "target": item.target.model_dump(),
                            "reason": item.reason,
                            "status": item.status.value,
                            "created_at": item.created_at.isoformat(),
                        }
                        for item in items
                    ],
                },
                ensure_ascii=False,
            )
        )
        return
    for item in items:
        typer.echo(
            f"{item.id} {item.kind.value} {item.target.namespace}/{item.target.name} "
            f"[{item.status.value}] 已等待 {_duration(item.created_at)} {item.reason}"
        )


@app.command()
def approve(
    proposal_id: str,
    actor: str = typer.Option("operator", help="审批人"),
    reason: str = typer.Option("", help="审批意见"),
    defer: bool = typer.Option(False, "--defer", help="只记录审批，稍后用 resume 执行"),
    output: OutputFormat = OutputFormat.TEXT,
) -> None:
    """批准并执行提案；执行前会再次校验目标，未知结果停止不重放。"""

    async def run():
        async with Store.open(_require_dsn()) as store:
            await store.migrate()
            async with _open_executor(Settings(), store) as executor:
                return await executor.approve(proposal_id, actor=actor, reason=reason, defer=defer)

    try:
        attempt = asyncio.run(run())
    except ActionError as exc:
        typer.echo(f"未执行（{exc.code}）：{exc.message}", err=True)
        raise typer.Exit(code=1) from exc
    except StoreUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    if attempt is None:
        typer.echo(f"提案 {proposal_id}：approved（未执行）")
        typer.echo(f"继续执行：aiops resume {proposal_id}")
        return
    _report_attempt(attempt, output)
    raise typer.Exit(code=0 if attempt.status.value == "verified" else 1)


@app.command()
def resume(
    proposal_id: str,
    actor: str = typer.Option("operator", help="继续执行的人"),
    output: OutputFormat = OutputFormat.TEXT,
) -> None:
    """继续执行已批准的提案；重新校验有效期、提案哈希与目标资源版本。"""

    async def run():
        async with Store.open(_require_dsn()) as store:
            await store.migrate()
            async with _open_executor(Settings(), store) as executor:
                return await executor.resume(proposal_id, actor=actor)

    try:
        attempt = asyncio.run(run())
    except ActionError as exc:
        typer.echo(f"未执行（{exc.code}）：{exc.message}", err=True)
        raise typer.Exit(code=1) from exc
    except StoreUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    _report_attempt(attempt, output)
    raise typer.Exit(code=0 if attempt.status.value == "verified" else 1)


@app.command("proposal")
def show_proposal(
    proposal_id: str,
    output: OutputFormat = OutputFormat.TEXT,
) -> None:
    """查看单个提案、最近一次审批与执行记录；用于重启后核对同一提案状态。"""

    async def run():
        async with Store.open(_require_dsn()) as store:
            proposal = await store.get_proposal(proposal_id)
            approval = await store.get_approval(proposal_id)
            attempt = await store.get_attempt(proposal_id)
            audit = await store.list_audit(proposal_id)
            return proposal, approval, attempt, audit

    try:
        proposal, approval, attempt, audit = asyncio.run(run())
    except StoreUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    if proposal is None:
        typer.echo(f"提案不存在：{proposal_id}", err=True)
        raise typer.Exit(code=1)
    if output == OutputFormat.JSON:
        typer.echo(
            json.dumps(
                {
                    "schema_version": "1",
                    "data": {
                        "proposal": proposal.model_dump(mode="json"),
                        "approval": approval.model_dump(mode="json") if approval else None,
                        "attempt": attempt.model_dump(mode="json") if attempt else None,
                        "audit": [event.model_dump(mode="json") for event in audit],
                    },
                },
                ensure_ascii=False,
            )
        )
        return

    typer.echo(
        f"{proposal.id} {proposal.kind.value} "
        f"{proposal.target.namespace}/{proposal.target.name} [{proposal.status.value}]"
    )
    if proposal.kind.value == "restart_pod":
        typer.echo(
            f"目标 Pod {proposal.target.name}，ReplicaSet "
            f"{proposal.parameters.get('replica_set')}，uid {proposal.target.uid}"
        )
    else:
        typer.echo(
            f"目标版本 revision {proposal.parameters.get('from_revision')} → "
            f"{proposal.parameters.get('to_revision')}，uid {proposal.target.uid}，"
            f"resourceVersion {proposal.target.resource_version}"
        )
    typer.echo(f"创建于 {proposal.created_at.isoformat()}，已等待 {_duration(proposal.created_at)}")
    if approval is None:
        typer.echo("审批：无")
    else:
        expires = approval.expires_at.isoformat() if approval.expires_at else "无"
        typer.echo(
            f"审批：{approval.decision.value} by {approval.actor} at "
            f"{approval.decided_at.isoformat()}，有效期至 {expires}"
        )
    if attempt is None:
        typer.echo("执行：尚未开始")
    else:
        typer.echo(f"执行：{attempt.id} {attempt.status.value}")
        typer.echo(f"验证：{json.dumps(attempt.verification, ensure_ascii=False)}")
    for event in audit:
        typer.echo(f"  {event.occurred_at.isoformat()} {event.actor} {event.event}")


@app.command()
def reject(
    proposal_id: str,
    actor: str = typer.Option("operator", help="审批人"),
    reason: str = typer.Option("", help="拒绝理由"),
) -> None:
    """拒绝提案；不执行任何集群写操作。"""

    async def run():
        async with Store.open(_require_dsn()) as store:
            await store.migrate()
            async with _open_executor(Settings(), store) as executor:
                return await executor.reject(proposal_id, actor=actor, reason=reason)

    try:
        proposal = asyncio.run(run())
    except ActionError as exc:
        typer.echo(f"未拒绝（{exc.code}）：{exc.message}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"提案 {proposal.id}：{proposal.status.value}")


if __name__ == "__main__":
    app()
