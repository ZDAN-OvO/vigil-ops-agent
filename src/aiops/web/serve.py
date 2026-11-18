"""同一事件循环内运行两个隔离的监听口和会话运行器。"""

import asyncio
import signal
from contextlib import nullcontext

import uvicorn

from aiops.config import Settings
from aiops.db import Store
from aiops.web.app import build_web_app
from aiops.web.runner import ConversationRunner
from aiops.webhook import build_app


class Server(uvicorn.Server):
    def capture_signals(self):
        return nullcontext()


async def serve_apps(
    settings: Settings,
    store: Store,
    runner: ConversationRunner,
    *,
    alert_host: str,
    alert_port: int,
) -> None:
    servers = [
        Server(
            uvicorn.Config(
                build_web_app(settings, store),
                host=settings.web_host,
                port=settings.web_port,
                proxy_headers=False,
                timeout_graceful_shutdown=3,
            )
        ),
        Server(
            uvicorn.Config(
                build_app(settings),
                host=alert_host,
                port=alert_port,
                timeout_graceful_shutdown=3,
            )
        ),
    ]
    loop = asyncio.get_running_loop()

    def stop() -> None:
        for server in servers:
            server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop)
    try:
        async with asyncio.TaskGroup() as group:
            worker = group.create_task(runner.run())

            async def run(server):
                try:
                    await server.serve()
                finally:
                    stop()
                    worker.cancel()

            for server in servers:
                group.create_task(run(server))
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
