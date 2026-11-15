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
from aiops.actions.executor import AutoExecutionError
from aiops.actions.rollout_undo import ActionError
from aiops.agent.engine import LeaseConflict, TaskNotResumable
from aiops.config import Settings
from aiops.contracts import (
    AuditEvent,
    Budget,
    FollowupStatus,
    Incident,
    Investigation,
    InvestigationStatus,
    ProposalStatus,
    TaskOutcome,
    TaskPhase,
    TerminationCode,
    TimeRange,
)
from aiops.db import Store, StoreUnavailable
from aiops.eval import evaluate, load_manifest
from aiops.investigation.baseline import RULES_VERSION
from aiops.investigation.explain import ExplainError, ExplainService
from aiops.investigation.factory import open_agent, open_baseline, open_executor
from aiops.knowledge import load_runbooks
from aiops.model import ModelClient
from aiops.report import Report
from aiops.webhook import build_app

app = typer.Typer(
    help="Vigil AIOps：故障调查与受控修复；实验命名空间的可丢弃 Pod 重建会被自动执行。",
    no_args_is_help=True,
    add_completion=False,
)
db_app = typer.Typer(help="实验 PostgreSQL 的迁移与连接检查。", no_args_is_help=True)
app.add_typer(db_app, name="db")


class OutputFormat(StrEnum):
    TEXT = "text"
    JSON = "json"


class InvestigationMode(StrEnum):
    AGENT = "agent"
    RULES = "rules"


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
        typer.echo(f"Vigil AIOps {__version__} · W0：调查、受控动作与实验命名空间的自动重建可用。")


def _report_payload(report: Report) -> dict:
    return {
        "schema_version": "1",
        "status": report.status.value,
        "investigation_id": report.investigation_id,
        "action_id": report.proposal_id,
        "data": {
            "task": report.task,
            "conclusion": report.conclusion,
            "termination_reason": report.termination_reason,
            "hypotheses": report.hypotheses,
            "evidence_count": report.evidence_count,
            "tool_call_count": report.tool_call_count,
            "action_ids": report.action_ids,
            "relations": report.relations,
            "prediction": report.prediction,
            "remaining_budget": report.remaining_budget,
            "needs_human": report.needs_human,
            "model_summary_completed": report.model_summary_completed,
        },
    }


def _print_report(report: Report, output: OutputFormat) -> None:
    if output == OutputFormat.JSON:
        typer.echo(json.dumps(_report_payload(report), ensure_ascii=False))
        return
    typer.echo(f"调查 {report.investigation_id}：{report.status.value}")
    task = report.task or {}
    if task:
        line = f"任务阶段 {task.get('phase')}"
        if task.get("outcome"):
            line += f"，结果 {task.get('outcome')}"
        if task.get("termination_code"):
            line += f"，停止原因 {task.get('termination_code')}"
        typer.echo(line)
        typer.echo(
            f"有效运行：调查 {task.get('investigation_seconds')}s、"
            f"动作 {task.get('action_seconds')}s，已过日历时间 {task.get('calendar_seconds')}s"
        )
    if report.proposal_id:
        typer.echo(f"动作提案：{report.proposal_id}")
    if report.conclusion:
        typer.echo(f"根因候选：{report.conclusion}")
    if report.termination_reason:
        typer.echo(f"终止原因：{report.termination_reason}")
    typer.echo(
        f"观测 {report.evidence_count} 条，工具调用 {report.tool_call_count} 次，"
        f"关系 {len(report.relations)} 条"
    )
    if report.prediction:
        typer.echo(
            "诊断预测："
            f"{report.prediction.get('prediction')} → 实际 {report.prediction.get('outcome')}"
            f"（{report.prediction.get('outcome_reason')}）"
        )
    if report.needs_human:
        typer.echo("人工待办：" + "；".join(report.needs_human))
    if not report.model_summary_completed:
        typer.echo("模型总结未完成；报告使用确定性结果保存真实状态。")
    for hypothesis in report.hypotheses:
        parent = f"（父 {hypothesis.get('parent_id')}）" if hypothesis.get("parent_id") else ""
        typer.echo(
            f"  - [{hypothesis['status']}] {hypothesis['statement']}"
            f"{parent} rev{hypothesis.get('revision', 1)}"
        )


@app.command()
def investigate(
    service: str = typer.Option(..., help="业务服务名"),
    symptoms: str = typer.Option(..., help="观察到的现象"),
    namespace: str = typer.Option("vigil-shop", help="业务命名空间"),
    cluster: str = typer.Option("vigil-lab", help="集群名，仅用于记录"),
    lookback_minutes: int = typer.Option(15, min=1, max=180, help="取证时间窗"),
    context: Annotated[list[str] | None, typer.Option(help="人工补充的上下文，可重复")] = None,
    mode: Annotated[
        InvestigationMode, typer.Option(help="agent 调用模型；rules 运行固定规则基线")
    ] = InvestigationMode.AGENT,
    output: OutputFormat = OutputFormat.TEXT,
) -> None:
    """运行一次调查；命中自动规则的可丢弃 Pod 重建会立即执行，其余只提案等待审批。"""
    settings = Settings()
    model_name = (
        f"rules:{RULES_VERSION}" if mode is InvestigationMode.RULES else settings.model_name
    )
    if not model_name:
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
        model=model_name,
        policy_version="mvp-1",
        budget=Budget(
            max_tool_calls=settings.investigation_budget_tool_calls,
            max_model_calls=settings.investigation_budget_model_calls,
            max_wall_clock_seconds=settings.investigation_budget_seconds,
            max_action_seconds=settings.investigation_budget_action_seconds,
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
                        payload={"service": service, "model": model_name, "mode": mode.value},
                    )
                ]
            )
            if mode is InvestigationMode.RULES:
                async with open_baseline(
                    settings=settings,
                    store=store,
                    incident=incident,
                    investigation=investigation,
                ) as baseline:
                    return await baseline.run()
            async with open_agent(
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
    except (LeaseConflict, TaskNotResumable) as exc:
        typer.echo(f"无法运行：{exc}", err=True)
        raise typer.Exit(code=1) from exc

    _print_report(report, output)
    raise typer.Exit(code=report.exit_code())


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
            observations = await store.list_observations(investigation_id)
            relations = await store.list_hypothesis_evidence(investigation_id)
            calls = await store.list_tool_calls(investigation_id)
            proposals = await store.list_proposals_for_investigation(investigation_id)
            task = await store.get_task(investigation_id)
        task_payload = None
        if task is not None:
            task_payload = {
                "phase": task.phase.value,
                "outcome": task.outcome.value if task.outcome else None,
                "termination_code": task.termination_code,
                "runtime_version": task.runtime_version,
                "proposal_id": task.proposal_id,
                "no_progress_rounds": task.no_progress_rounds,
                "recovered_at": task.recovered_at.isoformat() if task.recovered_at else None,
                "investigation_seconds": round(task.investigation_seconds, 1),
                "action_seconds": round(task.action_seconds, 1),
                "used_model_calls": task.used_model_calls,
                "used_tool_calls": task.used_tool_calls,
            }
        return Report(
            investigation_id=saved.id,
            status=saved.status,
            conclusion=saved.conclusion,
            termination_reason=saved.termination_reason,
            hypotheses=[
                {
                    "id": hypothesis.id,
                    "parent_id": hypothesis.parent_id,
                    "statement": hypothesis.statement,
                    "status": hypothesis.status.value,
                    "revision": hypothesis.revision,
                }
                for hypothesis in hypotheses
            ],
            evidence_count=len(observations),
            tool_call_count=len(calls),
            proposal_id=proposals[-1].id if proposals else None,
            task=task_payload,
            action_ids=[item.id for item in proposals],
            relations=[
                {
                    "hypothesis_id": item.hypothesis_id,
                    "observation_id": item.observation_id,
                    "relation": item.relation,
                    "rationale": item.rationale,
                }
                for item in relations
            ],
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
    async with open_executor(settings, store=store) as executor:
        yield executor


async def _agent_target(store: Store, proposal) -> tuple[Incident, Investigation] | None:
    """提案属于单 Agent 任务时返回事件与调查；旧提案返回 None。"""
    if proposal is None or not proposal.investigation_id:
        return None
    task = await store.get_task(proposal.investigation_id)
    if task is None:
        return None
    investigation = await store.get_investigation(proposal.investigation_id)
    if investigation is None:
        return None
    incident = await store.get_incident(investigation.incident_id)
    if incident is None:
        return None
    return incident, investigation


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


@app.command("eval")
def eval_report(
    manifest_path: str = typer.Option("eval/scenarios.json", help="冻结的评估清单"),
    output: OutputFormat = OutputFormat.TEXT,
) -> None:
    """按冻结清单核对 MVP 门槛；只读数据库记录，不触发运行。"""

    async def run():
        manifest = load_manifest(Path(manifest_path))
        async with Store.open(_require_dsn()) as store:
            return await evaluate(manifest, store)

    try:
        report = asyncio.run(run())
    except (OSError, ValueError) as exc:
        typer.echo(f"清单读取失败：{exc}", err=True)
        raise typer.Exit(code=2) from exc
    except StoreUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    if output == OutputFormat.JSON:
        typer.echo(
            json.dumps(
                {
                    "schema_version": "1",
                    "status": "passed" if report.passed else "failed",
                    "data": {
                        "frozen_at": report.frozen_at,
                        "criteria": [
                            {"id": item.id, "passed": item.passed, "detail": item.detail}
                            for item in report.criteria
                        ],
                    },
                },
                ensure_ascii=False,
            )
        )
    else:
        for item in report.criteria:
            mark = "通过" if item.passed else "未通过"
            typer.echo(f"{item.id} {mark}：{item.detail}")
    raise typer.Exit(code=0 if report.passed else 1)


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


def act_exit_code(proposal_status: str, attempt_status: str | None) -> int:
    """act 的退出码：0 成功、1 执行未成功、3 待审批、4 策略拒绝。"""
    if proposal_status == "denied":
        return 4
    if attempt_status is not None:
        return 0 if attempt_status == "verified" else 1
    return 3


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
    """提交一个受控动作；自动允许的当场执行，其余只提案等待审批。"""

    async def run():
        async with Store.open(_require_dsn()) as store:
            await store.migrate()
            async with _open_executor(Settings(), store) as executor:
                if kind == "restart-pod":
                    if not pod or not service:
                        raise ActionError("invalid_argument", "restart-pod 需要 --pod 与 --service")
                    proposal, decision = await executor.propose_restart_pod(
                        pod=pod,
                        service=service,
                        path=path,
                        reason=reason,
                        actor=actor,
                        namespace=namespace,
                        investigation_id=investigation,
                    )
                elif kind == "rollout-undo":
                    if not deployment:
                        raise ActionError("invalid_argument", "rollout-undo 需要 --deployment")
                    proposal, decision = await executor.propose_rollback(
                        deployment=deployment,
                        reason=reason,
                        actor=actor,
                        namespace=namespace,
                        investigation_id=investigation,
                    )
                else:
                    raise ActionError("invalid_argument", f"未知动作类型：{kind}")
                followups = await store.list_followups(proposal_id=proposal.id, limit=1)
                return (
                    proposal,
                    decision,
                    await store.get_attempt(proposal.id),
                    followups[0] if followups else None,
                )

    try:
        proposal, decision, attempt, followup = asyncio.run(run())
    except AutoExecutionError as exc:
        typer.echo(
            f"提案 {exc.proposal_id} 已保存但执行未完成（{exc.code}）：{exc.message}", err=True
        )
        raise typer.Exit(code=1) from exc
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
                        "attempt_id": attempt.id if attempt else None,
                        "attempt_status": attempt.status.value if attempt else None,
                        "outcome": (
                            attempt.verification.get("outcome")
                            if attempt and attempt.verification
                            else None
                        ),
                        "followup_id": followup.id if followup else None,
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
        if attempt is not None:
            typer.echo(f"执行 {attempt.id}：{attempt.status.value}")
            if attempt.verification:
                typer.echo(f"验证：{json.dumps(attempt.verification, ensure_ascii=False)}")
        if followup is not None:
            typer.echo(f"待办 {followup.id}：{followup.outcome.value}，{followup.reason}")
    raise typer.Exit(code=act_exit_code(proposal.status.value, attempt and attempt.status.value))


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
def followups(
    status: str = typer.Option("open", help="按状态过滤：open 或 resolved"),
    limit: int = typer.Option(20, min=1, max=200),
    output: OutputFormat = OutputFormat.TEXT,
) -> None:
    """列出异常执行留下的人工待办。"""
    try:
        wanted = FollowupStatus(status)
    except ValueError as exc:
        typer.echo(f"未知状态：{status}", err=True)
        raise typer.Exit(code=2) from exc

    async def run():
        async with Store.open(_require_dsn()) as store:
            return await store.list_followups(status=wanted, limit=limit)

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
                    "data": [item.model_dump(mode="json") for item in items],
                },
                ensure_ascii=False,
            )
        )
        return
    if not items:
        typer.echo("没有待办")
        return
    for item in items:
        typer.echo(
            f"{item.id} {item.outcome.value} {item.namespace}/{item.target_name} "
            f"[{item.status.value}] 提案 {item.proposal_id} "
            f"执行 {item.attempt_id or '无'} {item.reason}"
        )


@app.command()
def followup(
    followup_id: str,
    resolve: bool = typer.Option(False, "--resolve", help="标记为解决"),
    actor: str = typer.Option("operator", help="解决人"),
    note: str = typer.Option("", help="解决说明"),
    output: OutputFormat = OutputFormat.TEXT,
) -> None:
    """查看或解决一条人工待办。"""
    if resolve and not note.strip():
        typer.echo("--resolve 需要非空 --note", err=True)
        raise typer.Exit(code=2)
    if resolve and not actor.strip():
        typer.echo("--resolve 需要非空 --actor", err=True)
        raise typer.Exit(code=2)

    async def run():
        async with Store.open(_require_dsn()) as store:
            if resolve:
                return await store.resolve_followup(followup_id, actor=actor, note=note)
            return await store.get_followup(followup_id)

    try:
        item = asyncio.run(run())
    except StoreUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    if item is None:
        typer.echo(f"待办不存在或已经解决：{followup_id}", err=True)
        raise typer.Exit(code=1)
    if output == OutputFormat.JSON:
        typer.echo(
            json.dumps(
                {"schema_version": "1", "data": item.model_dump(mode="json")},
                ensure_ascii=False,
            )
        )
        return
    typer.echo(
        f"{item.id} {item.outcome.value} {item.namespace}/{item.target_name} [{item.status.value}]"
    )
    typer.echo(f"原因：{item.reason}")
    if item.resolved_at is not None:
        typer.echo(
            f"解决：{item.resolved_by} at {item.resolved_at.isoformat()} "
            f"{item.resolution or ''}".rstrip()
        )


@app.command()
def approve(
    proposal_id: str,
    actor: str = typer.Option("operator", help="审批人"),
    reason: str = typer.Option("", help="审批意见"),
    defer: bool = typer.Option(False, "--defer", help="只记录审批，稍后用 resume 继续"),
    output: OutputFormat = OutputFormat.TEXT,
) -> None:
    """批准提案：单 Agent 任务继续原任务；旧提案仍走原执行器并当场执行。"""

    async def run():
        settings = Settings()
        dsn = _require_dsn()
        async with Store.open(dsn) as store:
            await store.migrate()
            proposal = await store.get_proposal(proposal_id)
            if proposal is None:
                raise ActionError("not_found", f"提案不存在：{proposal_id}")
            target = await _agent_target(store, proposal)
            async with _open_executor(settings, store) as executor:
                if target is None:
                    attempt = await executor.approve(
                        proposal_id, actor=actor, reason=reason, defer=defer
                    )
                    return "legacy", attempt, None
                await executor.approve(proposal_id, actor=actor, reason=reason, defer=True)
            if defer:
                return "defer", None, None
            incident, investigation = target
            async with open_agent(
                settings=settings,
                store=store,
                dsn=dsn,
                incident=incident,
                investigation=investigation,
            ) as runtime:
                report = await runtime.resume(
                    proposal_id=proposal_id, decision="approved", actor=actor
                )
                return "agent", None, report

    try:
        mode, attempt, report = asyncio.run(run())
    except ActionError as exc:
        typer.echo(f"未执行（{exc.code}）：{exc.message}", err=True)
        raise typer.Exit(code=1) from exc
    except (LeaseConflict, TaskNotResumable) as exc:
        typer.echo(f"未继续（{exc.__class__.__name__}）：{exc}", err=True)
        raise typer.Exit(code=1) from exc
    except StoreUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    if mode == "agent" and report is not None:
        _print_report(report, output)
        raise typer.Exit(code=report.exit_code())
    if mode == "defer":
        typer.echo(f"提案 {proposal_id}：approved（未执行）")
        typer.echo(f"继续执行：aiops resume {proposal_id}")
        return
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
    """继续已批准提案：单 Agent 任务恢复原 checkpoint，旧提案走原执行器。"""

    async def run():
        settings = Settings()
        dsn = _require_dsn()
        async with Store.open(dsn) as store:
            await store.migrate()
            proposal = await store.get_proposal(proposal_id)
            if proposal is None:
                raise ActionError("not_found", f"提案不存在：{proposal_id}")
            target = await _agent_target(store, proposal)
            if target is None:
                async with _open_executor(settings, store) as executor:
                    attempt = await executor.resume(proposal_id, actor=actor)
                    return "legacy", attempt, None
            incident, investigation = target
            async with open_agent(
                settings=settings,
                store=store,
                dsn=dsn,
                incident=incident,
                investigation=investigation,
            ) as runtime:
                report = await runtime.resume(
                    proposal_id=proposal_id, decision="approved", actor=actor
                )
                return "agent", None, report

    try:
        mode, attempt, report = asyncio.run(run())
    except ActionError as exc:
        typer.echo(f"未执行（{exc.code}）：{exc.message}", err=True)
        raise typer.Exit(code=1) from exc
    except (LeaseConflict, TaskNotResumable) as exc:
        typer.echo(f"未继续（{exc.__class__.__name__}）：{exc}", err=True)
        raise typer.Exit(code=1) from exc
    except StoreUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    if mode == "agent" and report is not None:
        _print_report(report, output)
        raise typer.Exit(code=report.exit_code())
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
            followups = await store.list_followups(proposal_id=proposal_id)
            return proposal, approval, attempt, audit, followups

    try:
        proposal, approval, attempt, audit, followups = asyncio.run(run())
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
                        "followups": [item.model_dump(mode="json") for item in followups],
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
    for item in followups:
        typer.echo(f"待办：{item.id} {item.outcome.value} [{item.status.value}] {item.reason}")
    for event in audit:
        typer.echo(f"  {event.occurred_at.isoformat()} {event.actor} {event.event}")


@app.command()
def reject(
    proposal_id: str,
    actor: str = typer.Option("operator", help="审批人"),
    reason: str = typer.Option("", help="拒绝理由"),
    output: OutputFormat = OutputFormat.TEXT,
) -> None:
    """拒绝提案：新任务把拒绝结果交回 Agent 继续只读调查；不执行任何写操作。"""

    async def run():
        settings = Settings()
        dsn = _require_dsn()
        async with Store.open(dsn) as store:
            await store.migrate()
            proposal = await store.get_proposal(proposal_id)
            if proposal is None:
                raise ActionError("not_found", f"提案不存在：{proposal_id}")
            target = await _agent_target(store, proposal)
            async with _open_executor(settings, store) as executor:
                await executor.reject(proposal_id, actor=actor, reason=reason)
            if target is None:
                return proposal, None
            incident, investigation = target
            async with open_agent(
                settings=settings,
                store=store,
                dsn=dsn,
                incident=incident,
                investigation=investigation,
            ) as runtime:
                report = await runtime.resume(
                    proposal_id=proposal_id, decision="rejected", actor=actor
                )
                return proposal, report

    try:
        proposal, report = asyncio.run(run())
    except ActionError as exc:
        typer.echo(f"未拒绝（{exc.code}）：{exc.message}", err=True)
        raise typer.Exit(code=1) from exc
    except (LeaseConflict, TaskNotResumable) as exc:
        typer.echo(f"未继续（{exc.__class__.__name__}）：{exc}", err=True)
        raise typer.Exit(code=1) from exc
    except StoreUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"提案 {proposal.id}：rejected")
    if report is not None:
        _print_report(report, output)
        raise typer.Exit(code=report.exit_code())


@app.command()
def cancel(
    investigation_id: str,
    actor: str = typer.Option("operator", help="发起取消的人"),
    reason: str = typer.Option("", help="取消原因"),
    output: OutputFormat = OutputFormat.TEXT,
) -> None:
    """取消任务：持久化取消请求；运行中的进程在模型/工具检查点消费。"""

    async def run():
        dsn = _require_dsn()
        now = datetime.now(UTC)
        async with Store.open(dsn) as store:
            await store.migrate()
            task = await store.get_task(investigation_id)
            if task is None:
                raise TaskNotResumable("该调查不是单 Agent 任务")
            if task.phase is TaskPhase.TERMINAL:
                return task, False
            await store.request_cancel(investigation_id, at=now)
            if task.phase is TaskPhase.WAITING_APPROVAL:
                await store.set_investigation_status(
                    investigation_id,
                    InvestigationStatus.ABORTED,
                    termination_reason=reason or "任务已取消，未执行等待中的提案",
                    finished_at=now,
                )
                await store.finish_task(
                    investigation_id,
                    outcome=TaskOutcome.ABORTED,
                    termination_code=TerminationCode.CANCELLED.value,
                    finished_at=now,
                )
                await store.append_audit(
                    [
                        AuditEvent(
                            id=f"audit-{uuid.uuid4().hex[:12]}",
                            correlation_id=investigation_id,
                            actor=actor,
                            event="investigation.cancel_requested",
                            occurred_at=now,
                            payload={"reason": reason[:300], "phase": task.phase.value},
                        )
                    ]
                )
                refreshed = await store.get_task(investigation_id)
                return refreshed or task, True
            await store.append_audit(
                [
                    AuditEvent(
                        id=f"audit-{uuid.uuid4().hex[:12]}",
                        correlation_id=investigation_id,
                        actor=actor,
                        event="investigation.cancel_requested",
                        occurred_at=now,
                        payload={"reason": reason[:300], "phase": task.phase.value},
                    )
                ]
            )
            return task, True

    try:
        task, accepted = asyncio.run(run())
    except (TaskNotResumable, LookupError) as exc:
        typer.echo(f"无法取消：{exc}", err=True)
        raise typer.Exit(code=1) from exc
    except StoreUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    payload = {
        "schema_version": "1",
        "investigation_id": investigation_id,
        "status": task.phase.value,
        "cancelled": accepted,
        "data": {
            "phase": task.phase.value,
            "outcome": task.outcome.value if task.outcome else None,
            "termination_code": task.termination_code,
        },
    }
    if output == OutputFormat.JSON:
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    if not accepted:
        typer.echo(f"任务 {investigation_id} 已经是终态：{task.outcome}")
        return
    if task.phase is TaskPhase.TERMINAL:
        typer.echo(f"任务 {investigation_id} 已取消：{task.termination_code}")
    else:
        typer.echo(
            f"任务 {investigation_id} 记录了取消请求，运行中的进程会在下一个安全检查点停止。"
        )


if __name__ == "__main__":
    app()
