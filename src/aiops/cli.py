"""Vigil 的终端、一次性调用与管理入口。"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import re
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from aiops.actions.reconciliation import ActionReconciler, LegacyActionCutover
from aiops.actions.restart_pod import PodRestartAdapter
from aiops.actions.rollout_undo import ActionError, RolloutUndoAdapter
from aiops.actions.safety import ActionSafety
from aiops.agent.conversation import (
    AwaitingActionReceipt,
    ConversationDriver,
    ConversationResult,
    ConversationRun,
)
from aiops.config import Settings
from aiops.contracts import ApprovalDecision, FollowupStatus
from aiops.db import Store, StoreUnavailable, migration_files
from aiops.investigation.factory import open_conversation
from aiops.knowledge import load_runbooks
from aiops.sessions.checkpoint import setup_checkpoints
from aiops.sessions.models import LeaseLost, Session, SessionClosed, SessionEvent, TurnLane
from aiops.sessions.projection import project_action_result, project_payload, project_text
from aiops.sessions.store import SessionStore
from aiops.skills import SkillLoadError, load_skill_catalog

POLL_SECONDS = 0.5
TERMINAL_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


class CLIError(RuntimeError):
    """可以向调用者显示的运行错误。"""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        super().error(project_text(message))


def _terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("必须为正整数")
    return number


def _parse_args(argv: list[str], *, terminal: bool) -> argparse.Namespace:
    if argv and argv[0] in {"serve", "sessions", "admin"}:
        command, *rest = argv
        parser = _Parser(prog=f"vigil {command}")
        if command == "sessions":
            parser.add_argument("--limit", type=_positive, default=50)
        if command == "admin":
            admin = parser.add_subparsers(dest="operation", required=True)
            admin.add_parser("migrate", help="升级业务库与 checkpoint")
            admin.add_parser("doctor", help="检查配置、Skill 与数据库迁移")
            runbook = admin.add_parser("runbook", help="管理 runbook")
            commands = runbook.add_subparsers(dest="runbook_operation", required=True)
            commands.add_parser("import").add_argument("file", type=Path)
            followups = admin.add_parser("followups", help="查看人工待办")
            followups.add_argument("--status", type=FollowupStatus, choices=list(FollowupStatus))
            followups.add_argument("--proposal-id")
            followups.add_argument("--limit", type=_positive, default=50)
            reconcile = admin.add_parser("reconcile-action", help="核实锁定的动作")
            reconcile.add_argument("proposal_id")
            reconcile.add_argument(
                "--sender-stopped", action="store_true", help="确认原发送者已停止"
            )
            cutover = admin.add_parser("prepare-legacy-cutover", help="预览或准备旧库切换")
            cutover.add_argument("--apply", action="store_true")
            cutover.add_argument("--confirm", action="store_true")
            cutover.add_argument("--old-processes-stopped", action="store_true")
            cutover.add_argument("--restart-disabled", action="store_true")
            cutover.add_argument("--backup-complete", action="store_true")
        return argparse.Namespace(command=command, **vars(parser.parse_args(rest)))

    parser = _Parser(
        prog="vigil",
        description="调查集群问题。子命令：sessions、serve、admin。",
    )
    parser.add_argument("message", nargs="?", help="进入会话时发送的第一句话")
    parser.add_argument("-p", "--prompt", help="发送一条消息，运行一轮后退出")
    resume = parser.add_mutually_exclusive_group()
    resume.add_argument("--resume", metavar="SESSION_ID", help="恢复指定会话")
    resume.add_argument("-c", action="store_true", help="恢复最近会话")
    parser.add_argument("--output", choices=("text", "json", "stream-json"), default="text")
    args = parser.parse_args(argv)
    if args.prompt is not None and args.message is not None:
        parser.error("-p 与首条消息不能同时使用")
    args.message = args.prompt if args.prompt is not None else args.message
    if args.message is not None and not args.message.strip():
        parser.error("消息不能为空")
    args.interactive = terminal and args.prompt is None and args.output == "text"
    if not args.interactive and args.message is None and args.resume is None and not args.c:
        parser.error("一次性模式需要消息、--resume 或 -c")
    args.command = "chat"
    return args


def _diagnostic(message: str) -> None:
    print(TERMINAL_CONTROLS.sub("", project_text(message)), file=sys.stderr, flush=True)


class _ProjectedLogs(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        _diagnostic(record.getMessage())


def _error_message(exc: Exception) -> str:
    if isinstance(exc, (CLIError, ActionError, SkillLoadError)):
        return str(exc)
    if isinstance(exc, StoreUnavailable):
        return "无法连接 PostgreSQL，请检查 VIGIL_DATABASE_URL 与数据库状态"
    if isinstance(exc, SessionClosed):
        return "会话已关闭"
    if isinstance(exc, LeaseLost):
        return "已失去会话租约"
    if isinstance(exc, AwaitingActionReceipt):
        return "动作回执尚未收敛，稍后可恢复会话"
    return f"运行失败（{type(exc).__name__}）"


async def _write(line: str) -> None:
    await asyncio.to_thread(print, TERMINAL_CONTROLS.sub("", line), flush=True)


def _json(payload: Any, *, indent: int | None = None) -> str:
    line = json.dumps(project_payload(payload), ensure_ascii=False, indent=indent)
    return TERMINAL_CONTROLS.sub(lambda match: f"\\u{ord(match[0]):04x}", line)


def _local_event(type: str, session_id: str | None, **payload: Any) -> dict[str, Any]:
    return {
        **payload,
        "type": type,
        "session_id": session_id,
        "seq": None,
        "ts": datetime.now(UTC).isoformat(),
        "persisted": False,
    }


async def _events(
    sessions: SessionStore, session_id: str, *, after: int = 0, through: int | None = None
) -> AsyncIterator[SessionEvent]:
    while through is None or after < through:
        batch = await sessions.list_events(session_id, after_seq=after)
        if not batch:
            return
        for event in batch:
            if through is not None and event.seq > through:
                return
            yield event
        after = batch[-1].seq


def _text_event(event: SessionEvent) -> str | None:
    fields = event.payload
    match event.type:
        case "session":
            return f"会话 {event.session_id}  {fields['title']}"
        case "message":
            role = "你" if fields["role"] == "user" else "Vigil"
            if fields.get("lane") == "side":
                role = "Vigil（旁路）"
            return f"{role}> {fields['text']}"
        case "tool_call":
            summary = fields.get("summary") or ""
            return f"查询 {fields['tool']}  {fields['status']}  {summary}".rstrip()
        case "hypothesis":
            return f"假设  {fields['statement']}（{fields['status']}）"
        case "input_delivered":
            lane = "旁路" if fields.get("lane") == "side" else "主线"
            return f"线索已送达{lane}轮次 {event.turn_id} 的第 {fields['step']} 步"
        case "approval_required":
            return "审批卡片 " + _json(fields, indent=2)
        case "card_decision":
            return f"审批决定 {fields['card_id']}  {fields['status']}"
        case "action_result":
            return "动作回执 " + _json(fields, indent=2)
        case "result" if fields["exit_code"] == 1 or (
            fields["exit_code"] != 0 and fields.get("lane") != "side"
        ):
            return fields["summary"]
        case "session_closed":
            return "会话已关闭"
    return None


async def _emit_text(event: SessionEvent) -> None:
    line = _text_event(event)
    if line is not None:
        await _write(project_text(line))


class OnceOutput:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.session_id: str | None = None
        self.cursor = 0
        self.cards: list[SessionEvent] = []
        self.side_card: SessionEvent | None = None

    async def attach(self, session: Session, *, resumed: bool) -> None:
        self.session_id = session.id
        if resumed and self.mode == "stream-json":
            await _write(
                _json(
                    _local_event(
                        "session",
                        session.id,
                        source=session.source,
                        title=session.title,
                        resumed=True,
                    )
                )
            )
        if resumed and self.mode == "text":
            await _write(project_text(f"恢复会话 {session.id}  {session.title}"))

    async def emit(self, event: SessionEvent) -> None:
        if event.type == "approval_required":
            self.cards.append(event)
        if self.mode == "stream-json" and event.type != "result":
            await _write(_json(event.as_dict()))
        elif self.mode == "text" and event.type != "result":
            await _emit_text(event)

    async def read(
        self, sessions: SessionStore, *, through: int | None = None, stop_at_result: bool = False
    ) -> None:
        session_id = self.session_id
        assert session_id is not None
        async for event in _events(sessions, session_id, after=self.cursor, through=through):
            # 等驱动返回权威的结果序号，再越过可能属于另一轮的结果事件。
            if stop_at_result and event.type == "result":
                return
            await self.emit(event)
            self.cursor = event.seq

    async def finish(
        self,
        outcome: ConversationResult | None = None,
        *,
        status: str = "idle",
        summary: str = "没有待处理的轮次",
        exit_code: int = 0,
    ) -> int:
        result = outcome.result if outcome is not None else None
        payload = (
            result.as_dict()
            if result
            else _local_event(
                "result",
                self.session_id,
                status=status,
                summary=summary,
                exit_code=exit_code,
                schema_version=1,
            )
        )
        if self.mode == "json":
            side_card = self.side_card if result and result.payload.get("lane") == "side" else None
            payload["cards"] = [
                card.as_dict()
                for card in self.cards
                if card == side_card or (result and card.turn_id == result.turn_id)
            ]
            if side_card is not None:
                payload["card_id"] = side_card.payload["card_id"]
        if self.mode != "text":
            await _write(_json(payload))
        elif result is None:
            if exit_code == 0:
                await _write(project_text(summary))
        else:
            await _emit_text(result)
        return payload["exit_code"]


async def _find_session(sessions: SessionStore, args: argparse.Namespace) -> Session:
    if args.resume is not None:
        session = await sessions.get_session(args.resume)
        if session is None:
            raise CLIError("指定会话不存在")
        return session
    if args.c:
        recent = await sessions.list_sessions(limit=1)
        if not recent:
            raise CLIError("没有可恢复的会话")
        return recent[0]
    title = project_text(args.message or "终端会话")[:120]
    return await sessions.create_session(title=title)


async def _enqueue(sessions: SessionStore, session_id: str, message: str) -> None:
    await sessions.enqueue_input(session_id, message, display_text=project_text(message))


async def _turn_history(
    sessions: SessionStore, session: Session
) -> tuple[int, ConversationResult | None]:
    """恢复主线时从首条输入读起，保留该轮的卡片、决定与回执。"""
    turns = await sessions.list_turns(session.id)
    turn = next((turn for turn in reversed(turns) if turn.lane == "main"), None)
    if turn is None:
        return min(1, session.last_seq), None
    first = await sessions.get_input(turn.first_input_id)
    start = first.queued_seq - 1 if first is not None else 0
    card = result = None
    async for event in _events(sessions, session.id, after=start, through=session.last_seq):
        if event.turn_id != turn.id:
            continue
        if event.type == "approval_required":
            card = event
        elif event.type == "result":
            result = event
    return start, ConversationResult(result, card) if result is not None else None


async def _drive_once(
    run: ConversationRun, output: OnceOutput, sessions: SessionStore
) -> ConversationResult | None:
    task = asyncio.create_task(run.drive())
    interval = None if output.mode == "json" else POLL_SECONDS
    try:
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=interval)
            if done:
                break
            await output.read(sessions, stop_at_result=True)
        return task.result()
    finally:
        # 输出中断时仍等本轮收尾，然后退出持租约的上下文。
        if not task.done():
            await task
        elif not task.cancelled():
            task.exception()


async def _drive_side_input(
    driver: ConversationDriver, session_id: str, text: str, output: OnceOutput
) -> ConversationResult | None:
    sessions = driver.sessions
    incoming = await sessions.enqueue_input(session_id, text, display_text=project_text(text))
    deadline = asyncio.get_running_loop().time() + (
        driver.settings.investigation_budget_seconds
        + driver.settings.investigation_budget_action_seconds
    )
    cursor = incoming.queued_seq
    delivered_turn = None
    while asyncio.get_running_loop().time() < deadline:
        async for event in _events(sessions, session_id, after=cursor):
            cursor = event.seq
            if event.type == "input_delivered" and event.payload.get("input_id") == incoming.id:
                delivered_turn = event.turn_id
            if event.type == "result" and delivered_turn and event.turn_id == delivered_turn:
                return ConversationResult(event)
        session = await sessions.get_session(session_id)
        if session.state == "closed":
            return None
        current_input = await sessions.get_input(incoming.id)
        bound = (
            await sessions.get_turn(current_input.turn_id)
            if current_input is not None and current_input.turn_id
            else None
        )
        lane: TurnLane = (
            bound.lane if bound else "side" if session.state == "awaiting_approval" else "main"
        )
        with suppress(LeaseLost, SessionClosed, AwaitingActionReceipt):
            async with driver.claim(session_id, lane=lane) as run:
                if run is not None and await _drive_once(run, output, sessions) is not None:
                    continue
        await asyncio.sleep(POLL_SECONDS)
    raise CLIError("等待本次输入的结果超时")


async def _once(
    driver: ConversationDriver, session: Session, args: argparse.Namespace, output: OnceOutput
) -> ConversationResult | None:
    resumed = bool(args.resume is not None or args.c)
    await output.attach(session, resumed=resumed)
    sessions = driver.sessions
    lane: TurnLane = "side" if session.state == "awaiting_approval" and args.message else "main"
    history = None
    if resumed and (
        not args.message or session.state in {"working", "interrupted", "awaiting_approval"}
    ):
        output.cursor, history = await _turn_history(sessions, session)
        if lane == "side":
            if history is not None and history.card is not None:
                output.side_card = history.card
                await output.emit(history.card)
            output.cursor = session.last_seq
    elif resumed:
        output.cursor = session.last_seq

    if args.message and session.state == "closed":
        raise SessionClosed(session.id)
    if session.state == "closed":
        await output.read(sessions, through=session.last_seq)
        return None
    outcome = None
    if lane == "side":
        outcome = await _drive_side_input(driver, session.id, args.message, output)
        if outcome is None:
            raise SessionClosed(session.id)
    else:
        async with driver.claim(session.id, lane=lane) as run:
            if run is not None:
                if args.message:
                    await _enqueue(sessions, session.id, args.message)
                outcome = await _drive_once(run, output, sessions)
            elif args.message or session.state in {"working", "interrupted"}:
                raise CLIError("该轮次由另一进程驱动")
    outcome = outcome or history
    await output.read(sessions, through=outcome.result.seq if outcome else session.last_seq)
    return outcome


@asynccontextmanager
async def _read_terminal() -> AsyncIterator[asyncio.StreamReader]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    device = os.ttyname(sys.stdin.fileno())
    fd = os.open(device, os.O_RDONLY | os.O_NOCTTY)
    with os.fdopen(fd, "rb", buffering=0) as stream:
        transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), stream
        )
        try:
            yield reader
        finally:
            transport.close()


async def _pause(stop: asyncio.Event) -> None:
    with suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=POLL_SECONDS)


def _card_prefix(card_id: str) -> str:
    # act- 是所有动作 ID 的固定前缀，用后面的编号区分卡片。
    return card_id.removeprefix("act-")[:4]


class TerminalChat:
    def __init__(self, driver: ConversationDriver, session: Session) -> None:
        self.driver = driver
        self.sessions = driver.sessions
        self.session_id = session.id
        self.safety = ActionSafety(
            driver.store, approval_ttl_seconds=driver.settings.action_approval_ttl_seconds
        )
        self.stop = asyncio.Event()
        self.cursor = 0
        self.card: SessionEvent | None = None

    async def read_events(self) -> None:
        async for event in _events(self.sessions, self.session_id, after=self.cursor):
            await _emit_text(event)
            if event.type == "approval_required":
                card = await self.safety.get_card(
                    event.payload["card_id"], session_id=self.session_id
                )
                if card is not None and card["status"] == "pending_approval":
                    prefix = _card_prefix(event.payload["card_id"])
                    self.card = event
                    await _write(f"批准 /approve {prefix}  拒绝 /reject {prefix}")
            elif event.type == "session_closed" or (
                self.card is not None
                and event.type in {"card_decision", "action_result"}
                and event.payload["card_id"] == self.card.payload["card_id"]
            ):
                self.card = None
            self.cursor = event.seq

    async def follow(self) -> None:
        while not self.stop.is_set():
            await self.read_events()
            await _pause(self.stop)

    async def drive(self, lane: TurnLane) -> None:
        seen = -1
        while not self.stop.is_set():
            session = await self.sessions.get_session(self.session_id)
            if session.state == "closed":
                return
            if session.last_seq != seen:
                try:
                    async with self.driver.claim(self.session_id, lane=lane) as run:
                        if run is not None and not self.stop.is_set():
                            seen = session.last_seq
                            outcome = await run.drive()
                            while outcome is not None and outcome.card and not self.stop.is_set():
                                await _pause(self.stop)
                                if not self.stop.is_set():
                                    outcome = await run.drive()
                except SessionClosed:
                    return
                except (AwaitingActionReceipt, LeaseLost):
                    # 接手仍在验证的卡片时，让出会话租约并等待回执。
                    seen = -1
            await _pause(self.stop)

    async def handle(self, line: str) -> None:
        if not line:
            return
        command, *rest = line.split(maxsplit=1)
        if command in {"/approve", "/reject"}:
            card = self.card
            if not _terminal() or card is None or rest != [_card_prefix(card.payload["card_id"])]:
                _diagnostic("请输入当前待审批卡片的四位前缀")
                return
            decision = (
                ApprovalDecision.APPROVED if command == "/approve" else ApprovalDecision.REJECTED
            )
            try:
                await self.safety.record_card_decision(
                    card.payload["card_id"],
                    session_id=self.session_id,
                    fingerprint=card.payload["fingerprint"],
                    decision=decision,
                    actor=getpass.getuser(),
                )
                self.card = None
            except ActionError as exc:
                _diagnostic(_error_message(exc))
        else:
            try:
                await _enqueue(self.sessions, self.session_id, line)
            except SessionClosed as exc:
                _diagnostic(_error_message(exc))

    async def run(self, first: str | None) -> int:
        if first:
            await _enqueue(self.sessions, self.session_id, first)
        await self.read_events()
        await _write("输入消息开始对话，输入 /exit 或按 Ctrl+D 退出。")
        async with _read_terminal() as reader:
            workers = {
                asyncio.create_task(self.drive("main")),
                asyncio.create_task(self.drive("side")),
                asyncio.create_task(self.follow()),
            }
            pending_read = None
            try:
                while True:
                    pending_read = asyncio.create_task(reader.readline())
                    done, _ = await asyncio.wait(
                        workers | {pending_read}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if done & workers:
                        break
                    raw = pending_read.result()
                    line = raw.decode(sys.stdin.encoding or "utf-8", errors="replace").strip()
                    if not raw or line == "/exit":
                        break
                    await self.handle(line)
            finally:
                self.stop.set()
                if pending_read is not None:
                    pending_read.cancel()
                    await asyncio.gather(pending_read, return_exceptions=True)
                results = await asyncio.gather(*workers, return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException):
                        raise result
        await self.read_events()
        return 0


async def _migrate(store: Store) -> list[str]:
    applied = await store.migrate()
    await setup_checkpoints(store)
    return applied


async def _reconcile(store: Store, settings: Settings, args: argparse.Namespace) -> int:
    reconciler = ActionReconciler(
        store,
        adapter=RolloutUndoAdapter(
            kubeconfig=settings.kubeconfig, context_name=settings.kube_context
        ),
        restart_adapter=PodRestartAdapter(
            kubeconfig=settings.kubeconfig, context_name=settings.kube_context
        ),
    )
    proposal_id = args.proposal_id
    target = await reconciler.get_reconciliation(proposal_id)
    if target is None:
        raise CLIError("没有找到待核实动作")
    await _write(_json(target, indent=2))
    if target["target"]["cluster"] != settings.cluster:
        raise CLIError("当前集群与核实目标不一致")
    actor = getpass.getuser()
    prepared = await reconciler.prepare_reconciliation(
        proposal_id, actor=actor, sender_stopped=args.sender_stopped
    )
    await _write(_json(prepared, indent=2))
    try:
        written = input("写入情况 [written/not-written/unknown] ").strip()
        if written == "unknown":
            raise CLIError("写入情况仍待核实")
        if written not in {"written", "not-written"}:
            _diagnostic("写入情况必须为 written、not-written 或 unknown")
            return 2
        verified = input("验证通过 [yes/no] ").strip() if written == "written" else "no"
        if verified not in {"yes", "no"}:
            _diagnostic("验证结论必须为 yes 或 no")
            return 2
        note = input("核实依据 ").strip()
    except EOFError as exc:
        raise CLIError("未收到核实结论或依据") from exc
    receipt = await reconciler.reconcile_action(
        proposal_id,
        preparation_id=prepared["preparation_id"],
        actor=actor,
        written=written == "written",
        verified=verified == "yes",
        note=note,
    )
    await _write(_json(project_action_result(receipt), indent=2))
    return 0


async def _admin(store: Store, settings: Settings, args: argparse.Namespace) -> int:
    match args.operation:
        case "migrate":
            await _write(_json({"applied": await _migrate(store)}))
        case "doctor":
            catalog = load_skill_catalog(settings.skills_path)
            applied = await store.applied_migrations()
            missing = sorted({path.stem for path in migration_files()} - set(applied))
            await _write(
                _json(
                    {
                        "skills": len(catalog),
                        "missing_migrations": missing,
                        "model_configured": bool(settings.model_name),
                    }
                )
            )
            return int(bool(missing) or not settings.model_name)
        case "runbook":
            try:
                runbooks = load_runbooks(args.file)
            except (OSError, ValueError) as exc:
                raise CLIError(f"无法导入 runbook：{exc}") from exc
            for runbook in runbooks:
                await store.upsert_runbook(runbook)
            await _write(_json({"imported": len(runbooks)}))
        case "followups":
            rows = await store.list_followups(
                status=args.status, proposal_id=args.proposal_id, limit=args.limit
            )
            await _write(_json([row.model_dump(mode="json") for row in rows], indent=2))
        case "reconcile-action":
            return await _reconcile(store, settings, args)
        case "prepare-legacy-cutover":
            cutover = LegacyActionCutover(store)
            plan = await cutover.preview()
            await _write(_json(plan, indent=2))
            if args.apply:
                await cutover.prepare(
                    actor=getpass.getuser(),
                    confirmed=args.confirm,
                    old_processes_stopped=args.old_processes_stopped,
                    restart_disabled=args.restart_disabled,
                    backup_complete=args.backup_complete,
                )
                await _write(_json({"prepared": True}))
    return 0


async def _run(args: argparse.Namespace, terminal: bool, output: OnceOutput | None) -> int:
    settings = Settings()
    logger.remove()
    logger.add(
        lambda message: _diagnostic(message.record["message"]),
        level=settings.log_level,
        backtrace=False,
        diagnose=False,
    )
    logging.basicConfig(handlers=[_ProjectedLogs()], level=settings.log_level, force=True)
    logging.captureWarnings(True)
    if args.command == "serve":
        from aiops.serve import run_serve
        from aiops.web_assets import WebAssetsUnavailable

        try:
            await run_serve(settings, interactive=terminal)
        except WebAssetsUnavailable as exc:
            raise CLIError(str(exc)) from None
        return 0
    if not settings.database_url:
        raise CLIError("未配置 VIGIL_DATABASE_URL")
    async with Store.open(settings.database_url) as store:
        if args.command == "admin":
            return await _admin(store, settings, args)
        if args.command == "sessions":
            sessions = await SessionStore(store).list_sessions(limit=args.limit)
            for session in sessions:
                await _write(
                    project_text(
                        f"{session.id}\t{session.source}\t{session.state}\t{session.title}"
                    )
                )
            return 0
        await _migrate(store)
        if args.message and not settings.model_name:
            raise CLIError("未配置 VIGIL_MODEL_NAME")
        session = await _find_session(SessionStore(store), args)
        async with open_conversation(settings=settings, store=store) as driver:
            if output is None:
                return await TerminalChat(driver, session).run(args.message)
            outcome = await _once(driver, session, args, output)
    if outcome is None and session.state == "closed":
        return await output.finish(status="closed", summary="会话已关闭")
    return await output.finish(outcome)


async def _dispatch(args: argparse.Namespace, terminal: bool) -> int:
    output = OnceOutput(args.output) if args.command == "chat" and not args.interactive else None
    try:
        return await _run(args, terminal, output)
    except BrokenPipeError:
        raise
    except Exception as exc:
        message = _error_message(exc)
        _diagnostic(message)
        if output is not None:
            return await output.finish(status="failed", summary=message, exit_code=1)
        return 1


def main(argv: list[str] | None = None) -> int:
    terminal = _terminal()
    args = _parse_args(sys.argv[1:] if argv is None else argv, terminal=terminal)
    if args.command == "admin" and args.operation == "reconcile-action" and not terminal:
        _diagnostic("人工核实需要 stdin 和 stdout 都连接终端")
        return 2
    try:
        return asyncio.run(_dispatch(args, terminal))
    except KeyboardInterrupt:
        _diagnostic("运行已中断")
        return 1
    except BrokenPipeError:
        # 管道消费者退出后，避免解释器退出时再次刷新同一管道。
        with open(os.devnull, "wb") as sink:
            os.dup2(sink.fileno(), sys.stdout.fileno())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
