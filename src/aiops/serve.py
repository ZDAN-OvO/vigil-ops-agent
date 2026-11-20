"""管理本地监听与持久化会话工作。"""

import asyncio
import logging
import signal
import socket
import threading
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager

import uvicorn

from aiops.actions.safety import ActionSafety
from aiops.agent.conversation import AwaitingActionReceipt, ConversationDriver
from aiops.config import WEB_HOST, Settings
from aiops.db import Store
from aiops.monitoring.snapshot import Monitoring
from aiops.sessions.checkpoint import setup_checkpoints
from aiops.sessions.models import LeaseLost, SessionClosed, TurnLane
from aiops.web_api import BrowserAuth, build_web_app
from aiops.webhook import build_app

_LOG = logging.getLogger(__name__)
_SCAN_INTERVAL = 1.0


async def _wait_for_scan(stop: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(stop.wait(), _SCAN_INTERVAL)
    except TimeoutError:
        pass


async def _drive(
    driver: ConversationDriver,
    safety: ActionSafety,
    session_id: str,
    lane: TurnLane,
    stop: asyncio.Event,
) -> None:
    try:
        async with driver.claim(session_id, lane=lane) as run:
            if run is None or stop.is_set():
                return
            while not stop.is_set():
                result = await run.drive()
                if result is None or result.card is None:
                    return
                card_id = result.card.payload["card_id"]
                # 网页驱动持有当前主线租约，旁路由另一条任务独立领取。
                while not stop.is_set():
                    card = await safety.get_card(card_id, session_id=session_id)
                    if card is None:
                        return
                    if card["status"] != "pending_approval":
                        break
                    await _wait_for_scan(stop)
    except (LeaseLost, SessionClosed, AwaitingActionReceipt):
        # 数据库仍保留当前轮；下次扫描可在正确的租约和回执下继续。
        pass
    except Exception as exc:
        _LOG.error("会话驱动失败，稍后重试（%s）", type(exc).__name__)


async def drive_queue(
    driver: ConversationDriver, safety: ActionSafety, stop: asyncio.Event
) -> None:
    """扫描已提交的工作。退出时让在途动作完成验证和回执。"""
    running: dict[tuple[str, TurnLane], asyncio.Task[None]] = {}
    try:
        while not stop.is_set():
            running = {key: task for key, task in running.items() if not task.done()}
            try:
                await safety.recover_expired_targets()
                main, side, decided = await asyncio.gather(
                    driver.sessions.list_runnable_main(),
                    driver.sessions.list_runnable_side(),
                    safety.list_runnable_card_sessions(),
                )
                for lane, queued in (("main", [*decided, *main]), ("side", side)):
                    for session_id in queued:
                        key = (session_id, lane)
                        if key not in running and not stop.is_set():
                            running[key] = asyncio.create_task(
                                _drive(driver, safety, session_id, lane, stop)
                            )
            except Exception as exc:
                _LOG.error("会话扫描失败，稍后重试（%s）", type(exc).__name__)
            await _wait_for_scan(stop)
    finally:
        stop.set()
        await asyncio.gather(*running.values())


class _Server(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        # 两个监听共用 serve 的退出信号，由同一个入口安排关闭。
        yield


@contextmanager
def _shutdown_signals(stop: asyncio.Event) -> Iterator[None]:
    loop = asyncio.get_running_loop()
    handlers = {}
    try:
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                handlers[sig] = signal.getsignal(sig)
                loop.add_signal_handler(sig, stop.set)
        yield
    finally:
        for sig, previous in handlers.items():
            loop.remove_signal_handler(sig)
            signal.signal(sig, previous)


async def _listen(
    driver: ConversationDriver,
    safety: ActionSafety,
    settings: Settings,
    *,
    interactive: bool,
) -> None:
    stop = asyncio.Event()
    auth = BrowserAuth(interactive=interactive)
    monitoring = Monitoring.from_settings(settings)
    web = build_web_app(
        settings, sessions=driver.sessions, safety=safety, auth=auth, monitoring=monitoring.snapshot
    )
    listeners = [(web, WEB_HOST, settings.web_port)]
    if settings.webhook_host is not None:
        webhook = build_app(settings, sessions=driver.sessions, safety=safety)
        listeners.append((webhook, settings.webhook_host, settings.webhook_port))

    with ExitStack() as stack, _shutdown_signals(stop):
        bindings = []
        for app, host, port in listeners:
            family = socket.AF_INET6 if ":" in host else socket.AF_INET
            sock = stack.enter_context(socket.create_server((host, port), family=family))
            server = _Server(
                uvicorn.Config(
                    app,
                    host=host,
                    port=port,
                    access_log=False,
                    proxy_headers=False,
                    log_level=settings.log_level.lower(),
                    ws="none",
                )
            )
            bindings.append((server, sock))

        async with asyncio.TaskGroup() as tasks:

            async def serve(server: _Server, sock: socket.socket) -> None:
                try:
                    await server.serve(sockets=[sock])
                    if not stop.is_set():
                        raise RuntimeError("HTTP 监听意外停止")
                finally:
                    stop.set()

            for server, sock in bindings:
                tasks.create_task(serve(server, sock))
            tasks.create_task(drive_queue(driver, safety, stop))
            try:
                while not stop.is_set() and not all(server.started for server, _ in bindings):
                    await asyncio.sleep(0.01)
                if not stop.is_set() and (link := auth.login_url(settings.web_origin)):
                    print(f"Vigil 一次性网页登录链接：{link}", flush=True)
                await stop.wait()
            finally:
                stop.set()
                for server, _ in bindings:
                    server.should_exit = True


async def run_serve(settings: Settings, *, interactive: bool) -> None:
    """CLI 提供终端判定。启动前完成两个数据库账本的迁移。"""
    settings.validate_webhook_listener()
    if not settings.database_url:
        raise ValueError("未配置 VIGIL_DATABASE_URL")
    from aiops.investigation.factory import open_conversation

    async with Store.open(settings.database_url) as store:
        await store.migrate()
        await setup_checkpoints(store)
        safety = ActionSafety(
            store,
            web_url=settings.web_url,
            approval_ttl_seconds=settings.action_approval_ttl_seconds,
        )
        async with open_conversation(settings=settings, store=store) as driver:
            await _listen(driver, safety, settings, interactive=interactive)
