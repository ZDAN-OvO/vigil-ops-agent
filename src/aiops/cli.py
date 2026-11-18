"""Vigil 的服务、自检与开发命令。"""

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
from aiops.doctor import check_dependencies, mutating_webhook_present
from aiops.evaluation.harness import (
    ARMS,
    anchors,
    execute,
    load_records,
    merge_records,
    rescore,
    summarize,
    write_markdown,
    write_records,
)
from aiops.evaluation.scenarios import (
    SCENARIOS,
    ScenarioError,
    check_kube_context,
    get_scenario,
    scenario_payload,
)
from aiops.investigation.factory import arm_for_runtime, open_agent, open_executor
from aiops.investigation.rules import RULES_MODEL_NAME
from aiops.knowledge import load_runbooks
from aiops.process_lock import acquire
from aiops.report import Report, delegation_payloads
from aiops.web.runner import ConversationRunner
from aiops.web.serve import serve_apps

app = typer.Typer(
    help="Vigil AIOps：故障调查与受控修复。",
    no_args_is_help=True,
    add_completion=False,
)
dev_app = typer.Typer(help="开发、故障演练与对照评估。", no_args_is_help=True)
app.add_typer(dev_app, name="dev", hidden=True)
db_app = typer.Typer(help="实验 PostgreSQL 的迁移与连接检查。", no_args_is_help=True)
dev_app.add_typer(db_app, name="db")
lab_app = typer.Typer(help="对照评估：跑场景、看记录、比门槛。", no_args_is_help=True)
dev_app.add_typer(lab_app, name="lab")


class OutputFormat(StrEnum):
    TEXT = "text"
    JSON = "json"


class Arm(StrEnum):
    """对照臂：同一故障集上比较的三种调查方式。"""

    AGENT = "agent"
    GENERIC = "generic"
    RULES = "rules"


def _require_dsn() -> str:
    dsn = Settings().database_url
    if not dsn:
        typer.echo("未配置 VIGIL_DATABASE_URL，无法连接实验 PostgreSQL。", err=True)
        raise typer.Exit(code=2)
    return dsn


def _take_lock(
    *, serving: bool = False, inherit_lab_lock: bool = False, settings: Settings | None = None
) -> int:
    try:
        return acquire((settings or Settings()).runtime_path, inherit_lab_lock=inherit_lab_lock)
    except BlockingIOError as exc:
        message = (
            "已有 Vigil 进程持有推进者锁，无法启动 serve。"
            if serving
            else "Vigil 正在运行，请在页面上操作。"
        )
        typer.echo(message, err=True)
        raise typer.Exit(code=1) from exc
    except (OSError, ValueError) as exc:
        typer.echo("无法取得 Vigil 推进者锁，请检查运行目录。", err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def doctor() -> None:
    """检查运行依赖是否就绪，并给出修复提示。"""
    passed = True
    for name, ok, hint in asyncio.run(check_dependencies(Settings())):
        typer.echo(f"{'通过' if ok else '失败'}  {name}" + (f" · {hint}" if not ok else ""))
        passed &= ok
    if not passed:
        raise typer.Exit(code=1)


@db_app.command("migrate")
def db_migrate(output: OutputFormat = OutputFormat.TEXT) -> None:
    """应用尚未执行的业务表迁移；可重复执行。"""

    _take_lock()

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


@dev_app.command()
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
            "skills": report.skills,
            "delegations": report.delegations,
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
    if report.skills:
        typer.echo(
            "已加载 Skill："
            + "、".join(f"{item['name']}（{item['trigger']}）" for item in report.skills)
        )
    if report.delegations:
        typer.echo(f"委派调查 {len(report.delegations)} 次")
        for item in report.delegations:
            skills = "、".join(item.get("skills") or []) or "未记录"
            observation_ids = item.get("observation_ids") or []
            typer.echo(
                f"  - {item['id']} [{item['status']}] Skill：{skills}；"
                f"返回观测 {len(observation_ids)} 条"
            )
            typer.echo(f"    目标：{item['objective'][:80]}")
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
        typer.echo(f"  - [{hypothesis['status']}] {hypothesis['statement']}{parent}")


@dev_app.command()
def investigate(
    service: str = typer.Option(..., help="业务服务名"),
    symptoms: str = typer.Option(..., help="观察到的现象"),
    namespace: str = typer.Option("vigil-shop", help="业务命名空间"),
    cluster: str = typer.Option("vigil-lab", help="集群名，仅用于记录"),
    lookback_minutes: int = typer.Option(15, min=1, max=180, help="取证时间窗"),
    context: Annotated[list[str] | None, typer.Option(help="人工补充的上下文，可重复")] = None,
    arm: Annotated[Arm, typer.Option(help="对照臂：agent / generic / rules")] = Arm.AGENT,
    output: OutputFormat = OutputFormat.TEXT,
) -> None:
    """运行一次调查；命中自动规则的可丢弃 Pod 重建会立即执行，其余只提案等待审批。"""

    _take_lock(inherit_lab_lock=True)
    settings = Settings()
    model_name = RULES_MODEL_NAME if arm is Arm.RULES else settings.model_name
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
                        payload={"service": service, "model": model_name, "arm": arm.value},
                    )
                ]
            )
            async with open_agent(
                settings=settings,
                store=store,
                dsn=dsn,
                incident=incident,
                investigation=investigation,
                arm=arm.value,
            ) as runtime:
                return await runtime.run()

    try:
        report = asyncio.run(run())
    except StoreUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except (LeaseConflict, TaskNotResumable, ResumeNotSupported) as exc:
        typer.echo(f"无法运行：{exc}", err=True)
        raise typer.Exit(code=1) from exc

    _print_report(report, output)
    raise typer.Exit(code=report.exit_code())


@lab_app.command("scenarios")
def lab_scenarios(output: OutputFormat = OutputFormat.TEXT) -> None:
    """列出可注入、可核验、可复位的场景与判定口径。"""
    payload = [scenario_payload(spec) for spec in SCENARIOS]
    if output == OutputFormat.JSON:
        typer.echo(json.dumps({"schema_version": "1", "scenarios": payload}, ensure_ascii=False))
        return
    for item in payload:
        typer.echo(f"{item['id']}（{item['name']}）：{item['namespace']}/{item['service']}")
        typer.echo(f"  真值：{item['truth']}")
        action = item["expected_action"]
        if action == "none":
            typer.echo("  动作：正确做法是不动作")
        else:
            typer.echo(f"  动作：{action}@{item['expected_target']}")
        checks = "；".join(
            check["name"] + "(" + "/".join(check["tools"]) + ")"
            for check in item["evidence_checks"]
        )
        typer.echo(f"  证据检查：{checks}")


def _default_record_path() -> Path:
    today = datetime.now(UTC).date().isoformat()
    return Path("faults") / "runs" / f"{today}-comparison.json"


@lab_app.command("run")
def lab_run(
    scenario: Annotated[
        list[str] | None, typer.Option(help="场景 ID，可重复；留空表示全部场景")
    ] = None,
    arm: Annotated[
        list[Arm] | None, typer.Option(help="对照臂，可重复；留空表示三条臂都跑")
    ] = None,
    runs: int = typer.Option(3, min=1, max=20, help="每个（场景，臂）跑几次"),
    record: Annotated[
        Path | None, typer.Option(help="记录文件；默认 faults/runs/<日期>-comparison.json")
    ] = None,
    append: Annotated[bool, typer.Option(help="并入已有记录文件，而不是覆盖")] = False,
    output: OutputFormat = OutputFormat.TEXT,
) -> None:
    """按场景顺序跑对照实验；每步都调用 faults/ 下的脚本，不直接改集群。

    有效运行的定义：健康基线成立、注入成立、故障核验成立。任一步失败都记为无效，
    不计入分母以外的地方，也不删除记录。
    """

    lock_fd = _take_lock()
    dsn = _require_dsn()
    settings = Settings()
    specs = [get_scenario(name) for name in scenario] if scenario else list(SCENARIOS)
    arms = [item.value for item in (arm or [Arm.AGENT, Arm.GENERIC, Arm.RULES])]
    unknown = [name for name in arms if name not in ARMS]
    if unknown:
        typer.echo(f"未知对照臂：{', '.join(unknown)}", err=True)
        raise typer.Exit(code=2)
    try:
        context = check_kube_context(settings)
    except ScenarioError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"目标 context：{context}", err=True)
    batch_anchors = anchors(specs, settings.model_name)
    if batch_anchors.get("git_dirty") == "yes":
        typer.echo("警告：工作区有未提交改动，这批记录只能算探索性数据（ADR-0013 6.1）", err=True)

    target = record or _default_record_path()
    batch = {
        "date": datetime.now(UTC).date().isoformat(),
        "label": "同故障集对照",
        "scenarios": [spec.id for spec in specs],
        "arms": arms,
        "runs_per_cell": runs,
        "anchors": batch_anchors,
    }
    records: list[dict] = []

    def merged() -> tuple[dict, list[dict]]:
        if append and target.exists():
            return merge_records(json.loads(target.read_text()), batch, records)
        combined = dict(batch)
        combined["environment_restored"] = all(item["environment_restored"] for item in records)
        return combined, records

    def persist(item) -> None:  # noqa: ANN001 - 每跑一次就落盘，崩溃也不丢已完成运行
        records.append(item.as_dict())
        current_batch, current_records = merged()
        current_batch["environment_restored"] = all(
            record["environment_restored"] for record in current_records
        )
        write_records(target, current_batch, current_records)

    async def run_batch() -> None:
        async with Store.open(dsn) as store:
            await store.migrate()
            for spec in specs:
                for name in arms:
                    for index in range(1, runs + 1):
                        await execute(
                            store,
                            spec,
                            name,
                            index,
                            settings=settings,
                            on_record=persist,
                            lock_fd=lock_fd,
                        )

    asyncio.run(run_batch())
    batch, records = merged()
    summary = summarize(records)
    write_records(target, batch, records)
    write_markdown(target.with_suffix(".md"), batch, summary)

    if output == OutputFormat.JSON:
        typer.echo(
            json.dumps(
                {
                    "schema_version": "1",
                    "record": str(target),
                    "summary": summary,
                },
                ensure_ascii=False,
            )
        )
        return
    typer.echo(f"记录：{target}")
    typer.echo(f"汇总：{target.with_suffix('.md')}")
    typer.echo("")
    for row in summary["rows"]:
        typer.echo(
            f"{row['scenario']} / {row['arm']}：有效 {row['valid']}/{row['runs']}，"
            f"诊断正确 {row['diagnosis_correct']}，确认 {row['confirmed']}，"
            f"本臂恢复 {row['recovered_by_arm']}，越界 {row['boundary_violations']}"
        )
    for note in summary["comparison"]:
        typer.echo(f"- {note}")


@lab_app.command("rescore")
def lab_rescore(
    record: Annotated[Path, typer.Option(help="要重新判定的记录文件")],
    output: OutputFormat = OutputFormat.TEXT,
) -> None:
    """按当前判定口径重算已有记录；不重跑实验，也不改实验事实。"""
    try:
        payload = json.loads(record.read_text())
    except FileNotFoundError as exc:
        typer.echo(f"记录不存在：{record}", err=True)
        raise typer.Exit(code=2) from exc

    async def run() -> dict:
        async with Store.open(_require_dsn()) as store:
            return await rescore(store, payload)

    result = asyncio.run(run())
    batch = result.get("batch") or {}
    body = {
        "schema_version": result.get("schema_version", 1),
        "batch": batch,
        "summary": result["summary"],
        "records": result["records"],
    }
    record.write_text(json.dumps(body, ensure_ascii=False, indent=2, default=str) + "\n")
    write_markdown(record.with_suffix(".md"), batch, result["summary"])
    if output == OutputFormat.JSON:
        typer.echo(json.dumps(result["summary"], ensure_ascii=False))
        return
    typer.echo(f"已重算：{record}")
    for row in result["summary"]["rows"]:
        typer.echo(
            f"{row['scenario']} / {row['arm']}："
            f"诊断正确 {row['diagnosis_correct']}/{row['valid']}，"
            f"达到门槛 {row['meets_floor']}"
        )


@lab_app.command("summarize")
def lab_summarize(
    record: Annotated[Path, typer.Option(help="lab run 写出的 JSON 记录")],
    output: OutputFormat = OutputFormat.TEXT,
) -> None:
    """重新汇总一份已有记录；不重跑实验，也不修改记录。"""
    try:
        payload = load_records(record)
    except FileNotFoundError as exc:
        typer.echo(f"记录不存在：{record}", err=True)
        raise typer.Exit(code=2) from exc
    summary = payload.get("summary") or summarize(payload.get("records") or [])
    if output == OutputFormat.JSON:
        typer.echo(json.dumps(summary, ensure_ascii=False))
        return
    for row in summary["rows"]:
        typer.echo(
            f"{row['scenario']} / {row['arm']}：有效 {row['valid']}/{row['runs']}，"
            f"诊断正确率 {row['diagnosis_rate']:.2f}，达到门槛 {row['meets_floor']}"
        )
    for note in summary["comparison"]:
        typer.echo(f"- {note}")


@dev_app.command()
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


@dev_app.command()
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
            calls = await store.list_tool_requests(investigation_id)
            proposals = await store.list_proposals_for_investigation(investigation_id)
            delegations = await store.list_delegations(investigation_id)
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
                "calendar_seconds": round(
                    ((saved.finished_at or datetime.now(UTC)) - saved.started_at).total_seconds(),
                    1,
                ),
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
            skills=[item.model_dump(mode="json") for item in saved.skills],
            delegations=delegation_payloads(delegations, observations),
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
    host: str | None = typer.Option(None, help="告警监听地址，默认读配置"),
    port: int | None = typer.Option(None, help="告警监听端口，默认读配置"),
) -> None:
    """启动本机页面、后台调查与 AlertManager 告警接入。"""
    settings = Settings()
    if settings.webhook_token is None:
        typer.echo("未配置 VIGIL_WEBHOOK_TOKEN，拒绝启动无鉴权的 webhook。", err=True)
        raise typer.Exit(code=2)
    dsn = _require_dsn()
    if settings.model_name is None:
        typer.echo("未配置 VIGIL_MODEL_NAME，无法发起调查。", err=True)
        raise typer.Exit(code=2)

    _take_lock(serving=True, settings=settings)

    async def run() -> None:
        async with Store.open(dsn) as store:
            await store.migrate()
            runner = ConversationRunner(store, settings)
            await runner.startup()
            try:
                if await asyncio.to_thread(mutating_webhook_present, settings):
                    typer.echo("警告：存在可能改写 Deployment 的准入规则。", err=True)
            except Exception:
                typer.echo("警告：无法检查 Deployment 的可变更准入规则。", err=True)
            page_host = f"[{settings.web_host}]" if ":" in settings.web_host else settings.web_host
            typer.echo(f"Vigil 页面：http://{page_host}:{settings.web_port}/")
            await serve_apps(
                settings,
                store,
                runner,
                alert_host=host or settings.webhook_host,
                alert_port=settings.webhook_port if port is None else port,
            )

    try:
        asyncio.run(run())
    except StoreUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@asynccontextmanager
async def _open_executor(settings: Settings, store: Store):
    async with open_executor(settings, store=store) as executor:
        yield executor


async def _require_cli_investigation(store: Store, investigation_id: str | None) -> None:
    if investigation_id and await store.get_conversation_for_investigation(investigation_id):
        raise ActionError("web_owned", "此调查属于页面会话，请在页面操作。")


async def _arm_for_proposal(store: Store, proposal) -> str:
    """按任务记录还原提案所属的对照臂，避免恢复时把 generic 跑成 agent。"""
    if proposal is None or not proposal.investigation_id:
        return "agent"
    task = await store.get_task(proposal.investigation_id)
    return arm_for_runtime(task.runtime_version if task is not None else None)


class ResumeNotSupported(RuntimeError):
    """规则基线是同步运行，没有 checkpoint，不能按调查恢复。"""


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


@dev_app.command("knowledge")
def knowledge(
    action: str = typer.Argument("list", help="list 或 import"),
    path: str = typer.Option("runbooks", help="import 时的 JSON 文件或目录"),
) -> None:
    """登记与查看结构化 runbook；导入只写知识表，不改集群。"""

    if action == "import":
        _take_lock()

    async def run():
        async with Store.open(_require_dsn()) as store:
            if action == "import":
                await store.migrate()
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


@dev_app.command("act")
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

    _take_lock()

    async def run():
        async with Store.open(_require_dsn()) as store:
            await store.migrate()
            await _require_cli_investigation(store, investigation)
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


@dev_app.command()
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


@dev_app.command()
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


@dev_app.command("adopt-pending")
def adopt_pending() -> None:
    """把命令行中断留下、没有待办的未决执行转成人工待办，供 followup resolve 收尾。"""
    # 持锁保证原写命令进程已退出，这些记录不会再有人推进。
    _take_lock()

    async def run() -> list[str]:
        async with Store.open(_require_dsn()) as store:
            return await store.adopt_unfinished_attempts()

    try:
        created = asyncio.run(run())
    except StoreUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    if not created:
        typer.echo("没有需要转为待办的未决执行")
        return
    for followup_id in created:
        typer.echo(f"已生成待办 {followup_id}，核实后运行 aiops dev followup resolve {followup_id}")


@dev_app.command()
def followup(
    followup_id: str,
    resolve_id: str | None = typer.Argument(None, help="使用 followup resolve 时的待办 ID"),
    resolve: bool = typer.Option(False, "--resolve", help="标记为解决"),
    actor: str = typer.Option("operator", help="解决人"),
    note: str = typer.Option("", help="解决说明"),
    output: OutputFormat = OutputFormat.TEXT,
) -> None:
    """查看待办，或用 followup resolve <待办 ID> 记录核实结果。"""
    if resolve_id is not None:
        if followup_id != "resolve" or resolve:
            typer.echo("用法：followup resolve <待办 ID> --note <核实结果>", err=True)
            raise typer.Exit(code=2)
        followup_id, resolve = resolve_id, True
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


@dev_app.command()
def approve(
    proposal_id: str,
    actor: str = typer.Option("operator", help="审批人"),
    reason: str = typer.Option("", help="审批意见"),
    defer: bool = typer.Option(False, "--defer", help="只记录审批，稍后用 resume 继续"),
    output: OutputFormat = OutputFormat.TEXT,
) -> None:
    """批准提案：单 Agent 任务继续原任务；旧提案仍走原执行器并当场执行。"""

    _take_lock()

    async def run():
        settings = Settings()
        dsn = _require_dsn()
        async with Store.open(dsn) as store:
            await store.migrate()
            proposal = await store.get_proposal(proposal_id)
            if proposal is not None:
                await _require_cli_investigation(store, proposal.investigation_id)
            if proposal is None:
                raise ActionError("not_found", f"提案不存在：{proposal_id}")
            target = await _agent_target(store, proposal)
            arm = await _arm_for_proposal(store, proposal) if target is not None else "agent"
            if arm == "rules":
                raise ResumeNotSupported(
                    "规则基线调查没有可恢复的 checkpoint；需要执行时用 aiops dev act 重新提案"
                )
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
                arm=arm,
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
    except (LeaseConflict, TaskNotResumable, ResumeNotSupported) as exc:
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
        typer.echo(f"继续执行：aiops dev resume {proposal_id}")
        return
    if attempt is None:
        typer.echo(f"提案 {proposal_id}：approved（未执行）")
        typer.echo(f"继续执行：aiops dev resume {proposal_id}")
        return
    _report_attempt(attempt, output)
    raise typer.Exit(code=0 if attempt.status.value == "verified" else 1)


@dev_app.command()
def resume(
    proposal_id: str,
    actor: str = typer.Option("operator", help="继续执行的人"),
    output: OutputFormat = OutputFormat.TEXT,
) -> None:
    """继续已批准提案：单 Agent 任务恢复原 checkpoint，旧提案走原执行器。"""

    _take_lock()

    async def run():
        settings = Settings()
        dsn = _require_dsn()
        async with Store.open(dsn) as store:
            await store.migrate()
            proposal = await store.get_proposal(proposal_id)
            if proposal is not None:
                await _require_cli_investigation(store, proposal.investigation_id)
            if proposal is None:
                raise ActionError("not_found", f"提案不存在：{proposal_id}")
            target = await _agent_target(store, proposal)
            if target is None:
                async with _open_executor(settings, store) as executor:
                    attempt = await executor.resume(proposal_id, actor=actor)
                    return "legacy", attempt, None
            arm = await _arm_for_proposal(store, proposal)
            if arm == "rules":
                raise ResumeNotSupported(
                    "规则基线调查没有可恢复的 checkpoint；需要执行时用 aiops dev act 重新提案"
                )
            incident, investigation = target
            async with open_agent(
                settings=settings,
                store=store,
                dsn=dsn,
                incident=incident,
                investigation=investigation,
                arm=arm,
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
    except (LeaseConflict, TaskNotResumable, ResumeNotSupported) as exc:
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


@dev_app.command("proposal")
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


@dev_app.command()
def reject(
    proposal_id: str,
    actor: str = typer.Option("operator", help="审批人"),
    reason: str = typer.Option("", help="拒绝理由"),
    output: OutputFormat = OutputFormat.TEXT,
) -> None:
    """拒绝提案：新任务把拒绝结果交回 Agent 继续只读调查；不执行任何写操作。"""

    _take_lock()

    async def run():
        settings = Settings()
        dsn = _require_dsn()
        async with Store.open(dsn) as store:
            await store.migrate()
            proposal = await store.get_proposal(proposal_id)
            if proposal is not None:
                await _require_cli_investigation(store, proposal.investigation_id)
            if proposal is None:
                raise ActionError("not_found", f"提案不存在：{proposal_id}")
            target = await _agent_target(store, proposal)
            async with _open_executor(settings, store) as executor:
                await executor.reject(proposal_id, actor=actor, reason=reason)
            if target is None:
                return proposal, None
            arm = await _arm_for_proposal(store, proposal)
            if arm == "rules":
                raise ResumeNotSupported("规则基线调查没有可恢复的 checkpoint；拒绝只记录在提案上")
            incident, investigation = target
            async with open_agent(
                settings=settings,
                store=store,
                dsn=dsn,
                incident=incident,
                investigation=investigation,
                arm=arm,
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
    except (LeaseConflict, TaskNotResumable, ResumeNotSupported) as exc:
        typer.echo(f"未继续（{exc.__class__.__name__}）：{exc}", err=True)
        raise typer.Exit(code=1) from exc
    except StoreUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"提案 {proposal.id}：rejected")
    if report is not None:
        _print_report(report, output)
        raise typer.Exit(code=report.exit_code())


@dev_app.command()
def cancel(
    investigation_id: str,
    actor: str = typer.Option("operator", help="发起取消的人"),
    reason: str = typer.Option("", help="取消原因"),
    output: OutputFormat = OutputFormat.TEXT,
) -> None:
    """取消任务：持久化取消请求；运行中的进程在模型/工具检查点消费。"""

    _take_lock()

    async def run():
        dsn = _require_dsn()
        now = datetime.now(UTC)
        async with Store.open(dsn) as store:
            await store.migrate()
            await _require_cli_investigation(store, investigation_id)
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
    except (ActionError, TaskNotResumable, LookupError) as exc:
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
