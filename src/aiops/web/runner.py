"""单实例会话运行器。恢复只查看持久化状态，绝不重放写请求。"""

import asyncio
from contextlib import suppress
from uuid import uuid4

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from loguru import logger

from aiops.agent.schemas import ReportDraft
from aiops.config import Settings
from aiops.db import Store, StoreUnavailable
from aiops.investigation.factory import open_agent
from aiops.web.hints import confirm_hints
from aiops.web.lifecycle import handoff, recover_conversation


class ConversationRunner:
    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings
        self.instance_id = f"serve-{uuid4().hex}"
        self.active: set[str] = set()

    async def startup(self) -> None:
        if await self.store.has_unfinished_attempt(non_web_only=True):
            raise StoreUnavailable(
                "存在命令行来源或归属不明的未决执行，请用 aiops dev followups 核实并收尾。"
            )
        await self.recover_current()
        if await self.store.has_unfinished_attempt():
            raise StoreUnavailable("仍有未决执行，无法启动 serve。")
        async with self.store._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO web_serve_instance (instance_id) VALUES (%s)
                ON CONFLICT (singleton) DO UPDATE
                    SET instance_id = EXCLUDED.instance_id, started_at = now()
                """,
                (self.instance_id,),
            )

    async def reconcile_checkpoint(self, conversation: dict) -> None:
        """旧运行可能在最后一轮回复落盘后退出，尽量补记已送达的线索。"""
        if not await self.store.pending_conversation_hints(conversation["id"]):
            return
        async with self.store._pool.connection() as conn:
            cur = await conn.execute("SELECT to_regclass('checkpoints') IS NOT NULL AS exists")
            if not (await cur.fetchone())["exists"]:
                return
        async with AsyncPostgresSaver.from_conn_string(
            self.settings.database_url,
            serde=JsonPlusSerializer(allowed_msgpack_modules=[ReportDraft]),
        ) as saver:
            checkpoint = await saver.aget(
                {"configurable": {"thread_id": conversation["investigation_id"]}}
            )
        if checkpoint:
            await confirm_hints(self.store, conversation["id"], checkpoint["channel_values"])

    async def recover_current(self) -> dict | None:
        conversation = await self.store.get_current_conversation()
        if conversation is None or conversation["finished_at"] is not None:
            return None
        if conversation["investigation_id"] not in self.active:
            await self.reconcile_checkpoint(conversation)
        state = await recover_conversation(
            self.store,
            conversation["id"],
            self.instance_id,
            active=conversation["investigation_id"] in self.active,
        )
        return conversation if state == "queued" else None

    async def _renew(self, work: dict, lost: asyncio.Event) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                ok = await self.store.renew_web_lease(work["investigation_id"], self.instance_id)
            except Exception as exc:
                logger.error("页面调查租约续约异常：{}", type(exc).__name__)
                ok = False
            if not ok:
                logger.error("页面调查租约已失效：{}", work["investigation_id"])
                lost.set()
                return

    async def _drive(self, work: dict) -> None:
        investigation_id = work["investigation_id"]
        lost = asyncio.Event()
        self.active.add(investigation_id)
        renew = asyncio.create_task(self._renew(work, lost))
        try:
            investigation = await self.store.get_investigation(investigation_id)
            incident = await self.store.get_incident(investigation.incident_id)
            if work["kind"] == "start":
                await self.store.mark_investigation_running(
                    investigation_id, investigation.started_at
                )
            async with open_agent(
                settings=self.settings,
                store=self.store,
                dsn=self.settings.database_url,
                incident=incident,
                investigation=investigation,
                conversation_id=work["conversation_id"],
                serve_instance=self.instance_id,
                lease_lost=lost,
            ) as runtime:
                if work["kind"] == "start":
                    await runtime.run()
                else:
                    approval = await self.store.get_approval(work["proposal_id"])
                    if approval is None or not approval.card_hash:
                        raise RuntimeError("页面续跑缺少审批记录")
                    await runtime.resume(
                        proposal_id=approval.proposal_id,
                        decision=approval.decision.value,
                        actor=approval.actor,
                    )
                task = await self.store.get_task(investigation_id)
                if task.phase.value == "waiting_approval" and not lost.is_set():
                    await handoff(self.store, work, task.proposal_id)
                elif task.phase.value != "terminal":
                    raise RuntimeError("页面调查未完成交接")
        except Exception as exc:
            logger.error("页面调查推进异常：{}", type(exc).__name__)
            await self.store.release_task_lease(investigation_id, self.instance_id)
        finally:
            renew.cancel()
            with suppress(asyncio.CancelledError):
                await renew
            self.active.discard(investigation_id)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        next_recovery = 0.0
        in_progress: asyncio.Task | None = None
        try:
            while True:
                try:
                    if in_progress and in_progress.done():
                        finished, in_progress = in_progress, None
                        next_recovery = 0
                        await finished
                    if loop.time() >= next_recovery:
                        await self.recover_current()
                        next_recovery = loop.time() + 60
                    if in_progress is None:
                        current = await self.store.get_current_conversation()
                        if current and current["finished_at"] is None:
                            work = await self.store.claim_web_work(current["id"], self.instance_id)
                            if work:
                                in_progress = asyncio.create_task(self._drive(work))
                except Exception as exc:
                    logger.error("页面后台检查异常：{}", type(exc).__name__)
                    next_recovery = 0
                await asyncio.sleep(0.5)
        finally:
            if in_progress is not None:
                in_progress.cancel()
                with suppress(asyncio.CancelledError):
                    await in_progress
