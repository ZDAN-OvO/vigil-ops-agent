"""用同一 PostgreSQL 事务保存 LangGraph checkpoint、输入送达和会话事件。"""

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    DeltaChannelHistory,
)
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from aiops.db import Store
from aiops.sessions.models import Lease, LeaseLost, SessionInput, Turn
from aiops.sessions.store import RunTransaction, SessionStore

INPUT_IDS_CHANNEL = "vigil_input_ids"
TURN_ID_METADATA = "vigil_turn_id"


async def setup_checkpoints(store: Store) -> None:
    """用官方版本表升级。游离于业务事务，因为依赖包含并发建索引。"""
    # 独立连接在取消或断开时由 PostgreSQL 释放会话级锁。
    async with await store._pool.connection_class.connect(
        store._pool.conninfo, autocommit=True, **store._pool.kwargs
    ) as conn:
        lock = "hashtextextended(current_schema(), 2)"
        while True:
            result = await conn.execute(f"SELECT pg_try_advisory_lock({lock}) AS acquired")
            if (await result.fetchone())["acquired"]:
                break
            # 并发建索引会等待其它事务退出，不能在持有事务时等待咨询锁。
            await asyncio.sleep(0.05)
        await AsyncPostgresSaver(conn).setup()


class SessionCheckpointer(BaseCheckpointSaver):
    """限制写入到当前租约的线程，保持官方 saver 的数据格式与读写语义。"""

    def __init__(self, store: Store, lease: Lease, turn: Turn) -> None:
        self._sessions = SessionStore(store)
        self._lease = lease
        self._turn = turn
        self._reader = AsyncPostgresSaver(store._pool)
        super().__init__(serde=self._reader.serde)

    def _check_thread(self, config: RunnableConfig) -> None:
        if config["configurable"]["thread_id"] != self._turn.checkpoint_thread_id:
            raise LeaseLost(self._turn.id)

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        self._check_thread(config)
        return await self._reader.aget_tuple(config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        if config is None:
            config = {"configurable": {"thread_id": self._turn.checkpoint_thread_id}}
        self._check_thread(config)
        if before is not None:
            self._check_thread(before)
        async for item in self._reader.alist(config, filter=filter, before=before, limit=limit):
            yield item

    async def aget_delta_channel_history(
        self, *, config: RunnableConfig, channels: Sequence[str]
    ) -> Mapping[str, DeltaChannelHistory]:
        self._check_thread(config)
        return await self._reader.aget_delta_channel_history(config=config, channels=channels)

    def get_next_version(self, current: str | None, channel: None) -> str:
        return self._reader.get_next_version(current, channel)

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        self._check_thread(config)
        async with self._sessions.run(self._lease, self._turn.id) as tx:
            self._check_turn(tx)
            saver = AsyncPostgresSaver(tx.conn, serde=self.serde)
            saved_config = {
                "configurable": {
                    "thread_id": self._turn.checkpoint_thread_id,
                    "checkpoint_ns": config["configurable"]["checkpoint_ns"],
                    "checkpoint_id": checkpoint["id"],
                }
            }
            root = saved_config["configurable"]["checkpoint_ns"] == ""
            if await self._check_checkpoint_owner(tx, saved_config):
                # 提交回包丢失后，按原 ID 重试不会改写已提交的内容或重复发事件。
                return saved_config
            tagged_metadata = cast(CheckpointMetadata, {**metadata, TURN_ID_METADATA: tx.turn.id})
            saved_config = await saver.aput(config, checkpoint, tagged_metadata, new_versions)
            if root:
                claimed_ids = checkpoint["channel_values"].get(INPUT_IDS_CHANNEL)
                await self._deliver(tx, saver, saved_config, claimed_ids, metadata)
            return saved_config

    def _check_turn(self, tx: RunTransaction) -> None:
        if tx.turn.checkpoint_thread_id != self._turn.checkpoint_thread_id:
            raise LeaseLost(self._turn.id)

    @staticmethod
    async def _check_checkpoint_owner(tx: RunTransaction, config: RunnableConfig) -> bool:
        settings = config["configurable"]
        cursor = await tx.conn.execute(
            """
            SELECT metadata ->> %s AS turn_id FROM checkpoints
            WHERE thread_id = %s AND checkpoint_ns = %s AND checkpoint_id = %s
            """,
            (
                TURN_ID_METADATA,
                settings["thread_id"],
                settings["checkpoint_ns"],
                settings["checkpoint_id"],
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            return False
        if row["turn_id"] != tx.turn.id:
            raise LeaseLost(tx.turn.id)
        return True

    async def _deliver(
        self,
        tx: RunTransaction,
        saver: AsyncPostgresSaver,
        config: RunnableConfig,
        claimed_ids: Any,
        metadata: CheckpointMetadata,
    ) -> None:
        if claimed_ids is None:
            return
        if not isinstance(claimed_ids, list) or any(
            not isinstance(item, str) for item in claimed_ids
        ):
            raise ValueError("vigil_input_ids 必须是输入 ID 列表")
        if len(set(claimed_ids)) != len(claimed_ids):
            raise ValueError("checkpoint 含重复输入 ID")
        if not claimed_ids:
            return

        rows = await tx.conn.execute(
            """
            SELECT i.id, i.checkpoint_id, t.checkpoint_thread_id
            FROM session_inputs i LEFT JOIN session_turns t ON t.id = i.turn_id
            WHERE i.session_id = %s AND i.id = ANY(%s)
            """,
            (tx.turn.session_id, claimed_ids),
        )
        inputs = {row["id"]: row for row in await rows.fetchall()}
        pending_ids = []
        for input_id in claimed_ids:
            row = inputs.get(input_id)
            if row is None:
                raise ValueError(f"checkpoint 引用了其它会话或未知的输入：{input_id}")
            if row["checkpoint_id"] is None:
                pending_ids.append(input_id)
            elif row["checkpoint_thread_id"] != tx.turn.checkpoint_thread_id:
                raise ValueError(f"输入已送达其它线程：{input_id}")
        if not pending_ids:
            return
        pending = await tx.pending_inputs(limit=len(pending_ids))
        if pending_ids != [item.id for item in pending]:
            raise ValueError("checkpoint 必须按排队顺序送达当前轮可消费的输入")

        # 读回同事务中的根 checkpoint，同时验证 blob 版本确实保存了该列表。
        saved = await saver.aget_tuple(config)
        if (
            saved is None
            or saved.checkpoint["channel_values"].get(INPUT_IDS_CHANNEL) != claimed_ids
        ):
            raise ValueError("根 checkpoint 未持久化输入 ID")
        step = metadata.get("step")
        if type(step) is not int:
            raise ValueError("送达 checkpoint 必须记录整型步号")
        for incoming in pending:
            await self._mark_delivered(tx, incoming, config["configurable"]["checkpoint_id"], step)

    @staticmethod
    async def _mark_delivered(
        tx: RunTransaction, incoming: SessionInput, checkpoint_id: str, step: int
    ) -> None:
        await tx.conn.execute(
            """
            UPDATE session_inputs SET turn_id = %s, checkpoint_id = %s, delivered_step = %s
            WHERE id = %s
            """,
            (tx.turn.id, checkpoint_id, step, incoming.id),
        )
        await tx.append_event(
            "input_delivered",
            {
                "input_id": incoming.id,
                "step": step,
                "lane": tx.turn.lane,
            },
        )

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self._check_thread(config)
        async with self._sessions.run(self._lease, self._turn.id) as tx:
            self._check_turn(tx)
            await self._check_checkpoint_owner(tx, config)
            await AsyncPostgresSaver(tx.conn, serde=self.serde).aput_writes(
                config, writes, task_id, task_path
            )
