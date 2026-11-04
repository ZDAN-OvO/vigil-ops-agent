"""
Redis Checkpointer —— 模仿 LangGraph 官方 PostgreSQL Checkpointer 架构实现

本模块为 LangGraph 提供基于 Redis 的完整同步/异步检查点持久化存储，
架构对标 ``langgraph.checkpoint.postgres`` 的三层设计：

┌─────────────────────────────────────────────────────────┐
│  BaseRedisSaver (抽象基类)                               │
│  - serde 序列化 / 反序列化                                │
│  - _dump_blobs / _load_blobs / _dump_writes / _load_writes│
│  - get_next_version / _metadata_matches                  │
├──────────────────────┬──────────────────────────────────┤
│  RedisSaver (同步)    │  AsyncRedisSaver (异步)           │
│  - redis.Redis       │  - redis.asyncio.Redis            │
│  - threading.Lock    │  - asyncio.Lock                   │
│  - get_tuple / list  │  - aget_tuple / alist             │
│  - put / put_writes  │  - aput / aput_writes             │
│  - delete_thread     │  - adelete_thread                 │
│                      │  - 同步包装器 (跨线程委托)          │
└──────────────────────┴──────────────────────────────────┘

Redis Key 设计（单 Hash 合并模型）：
─────────────────────────────────────
每个 thread + namespace 只使用 **1 个 Redis Hash key**：

    ckp:{thread_id}:{checkpoint_ns}

Hash 内部通过 field 前缀区分不同类型的数据：

┌────────────────────────────────────┬──────────────────────────────────┐
| Field 格式                         | 存储内容                          │
├────────────────────────────────────┼──────────────────────────────────┤
| c:{checkpoint_id}                  | checkpoint 结构 (base64 msgpack) │
| t:{checkpoint_id}                  | checkpoint serde type tag        │
| p:{checkpoint_id}                  | parent_checkpoint_id             │
| m:{checkpoint_id}                  | metadata (base64 msgpack)        │
| mt:{checkpoint_id}                 | metadata serde type tag          │
| b:{channel}:{version}              | channel blob (type:b64)          │
| w:{checkpoint_id}:{task_id}:{idx}  | write 数据 (JSON)                │
| __order__                          | checkpoint_ids (JSON, 时间倒序)  │
└────────────────────────────────────┴──────────────────────────────────┘

优势：
- 10 轮对话 = 1 个 key（而非 22 个），大幅减少 Redis key 元数据开销
- TTL 只需设置 1 次
- delete_thread 只需 1 次 DEL，无 SCAN
- 所有数据在同一 Hash 中，读取效率高（Redis Hash ziplist 编码）

序列化说明：
- checkpoint 结构和 metadata 使用 LangGraph 内置 serde 序列化
- channel blob 使用 serde.dumps_typed / loads_typed
- 所有二进制数据使用 base64 编码后存储
- 原始类型（str/int/float/bool/None）内联到 checkpoint 结构中
- writes 的元数据以 JSON 格式存储
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import threading
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import contextmanager
from typing import Any, Optional, Union, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_serializable_checkpoint_metadata,
)
from langgraph.checkpoint.serde.base import SerializerProtocol
from langgraph.checkpoint.serde.types import _DeltaSnapshot
from loguru import logger

# ─────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────
_KEY_PREFIX = "ckp:"
_ORDER_FIELD = "__order__"

MetadataInput = dict[str, Any] | None


# ═════════════════════════════════════════════
# BaseRedisSaver —— 共享基类
# ═════════════════════════════════════════════
class BaseRedisSaver(BaseCheckpointSaver[str]):
    """Redis Checkpointer 抽象基类

    封装所有与 Redis 客户端类型无关的共享逻辑。
    """

    # ── Key 构造 ──────────────────────────────

    @staticmethod
    def _key(thread_id: str, checkpoint_ns: str) -> str:
        """构建唯一的 Hash key（每个 thread+ns 只有一个）"""
        return f"{_KEY_PREFIX}{thread_id}:{checkpoint_ns}"

    # ── Field 构造 ────────────────────────────

    @staticmethod
    def _cp_field(checkpoint_id: str) -> str:
        """checkpoint 结构 field"""
        return f"c:{checkpoint_id}"

    @staticmethod
    def _cp_type_field(checkpoint_id: str) -> str:
        """checkpoint serde type field"""
        return f"t:{checkpoint_id}"

    @staticmethod
    def _cp_parent_field(checkpoint_id: str) -> str:
        """parent_checkpoint_id field"""
        return f"p:{checkpoint_id}"

    @staticmethod
    def _meta_field(checkpoint_id: str) -> str:
        """metadata field"""
        return f"m:{checkpoint_id}"

    @staticmethod
    def _meta_type_field(checkpoint_id: str) -> str:
        """metadata serde type field"""
        return f"mt:{checkpoint_id}"

    @staticmethod
    def _blob_field(channel: str, version: str) -> str:
        """channel blob field"""
        return f"b:{channel}:{version}"

    @staticmethod
    def _write_field(checkpoint_id: str, task_id: str, idx: int) -> str:
        """write field"""
        return f"w:{checkpoint_id}:{task_id}:{idx}"

    # ── 序列化辅助 ────────────────────────────

    @staticmethod
    def _encode_blob(type_tag: str, blob: bytes | None) -> str:
        """将 serde 输出编码为 Redis 可存储的字符串"""
        if blob is None:
            return "empty:"
        return f"{type_tag}:{base64.b64encode(blob).decode('utf-8')}"

    @staticmethod
    def _decode_blob(encoded: str) -> tuple[str, bytes | None]:
        """将 Redis 中存储的 blob 字符串解码"""
        if encoded.startswith("empty"):
            return ("empty", None)
        type_tag, b64_data = encoded.split(":", 1)
        return (type_tag, base64.b64decode(b64_data))

    # ── Blob dump / load ──────────────────────

    def _dump_blobs(
        self,
        values: dict[str, Any],
        versions: ChannelVersions,
    ) -> dict[str, str]:
        """将 channel values 序列化为 Hash fields

        返回 {field: encoded_value} 字典
        """
        if not versions:
            return {}

        fields: dict[str, str] = {}
        for channel, version in versions.items():
            field = self._blob_field(channel, str(version))
            if channel in values:
                type_tag, raw_bytes = self.serde.dumps_typed(values[channel])
                fields[field] = self._encode_blob(type_tag, raw_bytes)
            else:
                fields[field] = "empty:"
        return fields

    def _load_blobs(
        self,
        hash_data: dict[str, str],
        channel_versions: dict[str, Any],
        inline_channels: set[str],
    ) -> dict[str, Any]:
        """从 Hash 数据中提取并反序列化 channel blobs

        Args:
            hash_data: 完整的 Hash 数据（hgetall 结果）
            channel_versions: channel → version 映射
            inline_channels: 已内联到 checkpoint 中的 channel 集合
        """
        result: dict[str, Any] = {}
        for channel, version in channel_versions.items():
            if channel in inline_channels:
                continue
            field = self._blob_field(channel, str(version))
            encoded = hash_data.get(field)
            if encoded is None:
                continue
            type_tag, blob = self._decode_blob(encoded)
            if type_tag == "empty" or blob is None:
                continue
            try:
                result[channel] = self.serde.loads_typed((type_tag, blob))
            except Exception as e:
                logger.warning(f"反序列化 blob 失败: channel={channel}, error={e}")
        return result

    # ── Writes dump / load ────────────────────

    def _dump_writes(
        self,
        checkpoint_id: str,
        task_id: str,
        task_path: str,
        writes: Sequence[tuple[str, Any]],
    ) -> dict[str, str]:
        """将中间写入序列化为 Hash fields"""
        fields: dict[str, str] = {}
        for idx, (channel, value) in enumerate(writes):
            write_idx = WRITES_IDX_MAP.get(channel, idx)
            field = self._write_field(checkpoint_id, task_id, write_idx)
            type_tag, raw_bytes = self.serde.dumps_typed(value)
            fields[field] = json.dumps(
                {
                    "channel": channel,
                    "type": type_tag,
                    "blob": base64.b64encode(raw_bytes).decode("utf-8"),
                    "task_path": task_path,
                }
            )
        return fields

    def _load_writes(
        self,
        hash_data: dict[str, str],
        checkpoint_id: str,
    ) -> list[tuple[str, str, Any]]:
        """从 Hash 数据中提取并反序列化 writes"""
        prefix = f"w:{checkpoint_id}:"
        result: list[tuple[str, str, Any]] = []

        for field, json_str in hash_data.items():
            if not field.startswith(prefix):
                continue
            # field 格式: w:{checkpoint_id}:{task_id}:{idx}
            # 提取 task_id: 去掉 "w:{checkpoint_id}:" 前缀，再 rsplit 取第一部分
            remainder = field[len(prefix) :]
            task_id = remainder.rsplit(":", 1)[0]

            try:
                data = json.loads(json_str)
                channel = data["channel"]
                type_tag = data["type"]
                blob = base64.b64decode(data["blob"])
                value = self.serde.loads_typed((type_tag, blob))
                result.append((task_id, channel, value))
            except Exception as e:
                logger.warning(f"反序列化 write 失败: field={field}, error={e}")
        return result

    # ── 版本管理 ──────────────────────────────

    def get_next_version(self, current: str | None, channel: None) -> str:
        """生成下一个 channel 版本号

        格式: ``{version:032}.{random:016}``
        """
        if current is None:
            current_v = 0
        elif isinstance(current, int):
            current_v = current
        else:
            current_v = int(str(current).split(".")[0])
        next_v = current_v + 1
        next_h = random.random()
        return f"{next_v:032}.{next_h:016}"

    # ── metadata 过滤 ─────────────────────────

    @staticmethod
    def _metadata_matches(metadata: dict[str, Any], filter: dict[str, Any]) -> bool:
        """检查 metadata 是否满足过滤条件（子集匹配）"""
        for key, value in filter.items():
            if key not in metadata:
                return False
            if isinstance(value, dict):
                if not isinstance(metadata.get(key), dict):
                    return False
                if not BaseRedisSaver._metadata_matches(metadata[key], value):
                    return False
            elif metadata[key] != value:
                return False
        return True

    # ── CheckpointTuple 构建 ──────────────────

    def _build_checkpoint_tuple(
        self,
        hash_data: dict[str, str],
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
    ) -> CheckpointTuple:
        """从 Hash 数据构建 CheckpointTuple"""
        # 反序列化 checkpoint 结构
        cp_field = self._cp_field(checkpoint_id)
        cp_type_field = self._cp_type_field(checkpoint_id)
        cp_type = hash_data.get(cp_type_field, "msgpack")
        checkpoint = self.serde.loads_typed((cp_type, base64.b64decode(hash_data[cp_field])))

        # 反序列化 metadata
        meta_field = self._meta_field(checkpoint_id)
        meta_type_field = self._meta_type_field(checkpoint_id)
        metadata = {}
        if meta_field in hash_data:
            md_type = hash_data.get(meta_type_field, "msgpack")
            metadata = self.serde.loads_typed((md_type, base64.b64decode(hash_data[meta_field])))

        # parent
        parent_field = self._cp_parent_field(checkpoint_id)
        parent_checkpoint_id = hash_data.get(parent_field) or None

        # 加载 channel blobs
        channel_versions = checkpoint.get("channel_versions", {})
        inline_values = checkpoint.get("channel_values", {}) or {}
        inline_channels = set(inline_values.keys())
        blob_values = self._load_blobs(hash_data, channel_versions, inline_channels)

        # 合并 inline 和 blob
        channel_values = {**inline_values, **blob_values}

        # 加载 writes
        write_values = self._load_writes(hash_data, checkpoint_id)

        # 构建 config
        checkpoint_config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

        parent_config = None
        if parent_checkpoint_id:
            parent_config = {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": parent_checkpoint_id,
                }
            }

        return CheckpointTuple(
            config=checkpoint_config,
            checkpoint={**checkpoint, "channel_values": channel_values},
            metadata=metadata,
            parent_config=parent_config,
            pending_writes=write_values or None,
        )

    # ── 内部辅助 ──────────────────────────────

    @staticmethod
    def _parse_order(hash_data: dict[str, str]) -> list[str]:
        """从 Hash 中解析 checkpoint 顺序列表"""
        order_json = hash_data.get(_ORDER_FIELD, "[]")
        try:
            return json.loads(order_json)
        except Exception:
            return []

    @staticmethod
    def _update_order(
        current_order: list[str],
        new_checkpoint_id: str,
    ) -> list[str]:
        """更新 checkpoint 顺序列表（新 checkpoint 插入头部）"""
        # 移除已存在的（如果有）
        order = [cid for cid in current_order if cid != new_checkpoint_id]
        # 插入到头部（最新）
        order.insert(0, new_checkpoint_id)
        return order


# ═════════════════════════════════════════════
# RedisSaver —— 同步实现
# ═════════════════════════════════════════════
class RedisSaver(BaseRedisSaver):
    """同步 Redis Checkpointer

    对标 ``langgraph.checkpoint.postgres.PostgresSaver``

    用法::

        import redis
        conn = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)
        saver = RedisSaver(conn)
        saver.setup()
    """

    def __init__(
        self,
        conn: Any,  # redis.Redis
        *,
        serde: SerializerProtocol | None = None,
        ttl: int = 0,
    ) -> None:
        super().__init__(serde=serde)
        self.conn = conn
        self.lock = threading.Lock()
        self.ttl = ttl

    @classmethod
    @contextmanager
    def from_conn_info(
        cls,
        host: str = "localhost",
        port: int = 6379,
        db: int = 0,
        password: str | None = None,
        *,
        ttl: int = 0,
        serde: SerializerProtocol | None = None,
    ) -> Iterator[RedisSaver]:
        """从连接信息创建 RedisSaver 实例"""
        import redis as sync_redis

        conn = sync_redis.Redis(
            host=host,
            port=port,
            db=db,
            password=password or None,
            decode_responses=True,
        )
        try:
            conn.ping()
            yield cls(conn, ttl=ttl, serde=serde)
        finally:
            conn.close()

    def setup(self) -> None:
        """初始化检查点存储"""
        self.conn.ping()
        logger.info("Redis Checkpointer 同步初始化完成")

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """同步获取 checkpoint"""
        thread_id = config["configurable"]["thread_id"]
        checkpoint_id = get_checkpoint_id(config)
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        key = self._key(thread_id, checkpoint_ns)

        if checkpoint_id:
            # 获取指定 checkpoint
            cp_field = self._cp_field(checkpoint_id)
            if not self.conn.hexists(key, cp_field):
                return None
            hash_data = self.conn.hgetall(key)
            if not hash_data:
                return None
        else:
            # 获取最新 checkpoint
            hash_data = self.conn.hgetall(key)
            if not hash_data:
                return None
            order = self._parse_order(hash_data)
            if not order:
                return None
            checkpoint_id = order[0]

        return self._build_checkpoint_tuple(hash_data, thread_id, checkpoint_ns, checkpoint_id)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        """同步列出 checkpoints"""
        if config is None:
            return

        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        key = self._key(thread_id, checkpoint_ns)

        hash_data = self.conn.hgetall(key)
        if not hash_data:
            return

        order = self._parse_order(hash_data)
        if not order:
            return

        before_id = get_checkpoint_id(before) if before else None
        count = 0

        for cp_id in order:
            if before_id and cp_id >= before_id:
                continue
            if limit is not None and count >= limit:
                break

            # metadata 过滤
            if filter:
                meta_field = self._meta_field(cp_id)
                meta_type_field = self._meta_type_field(cp_id)
                if meta_field in hash_data:
                    md_type = hash_data.get(meta_type_field, "msgpack")
                    metadata = self.serde.loads_typed(
                        (md_type, base64.b64decode(hash_data[meta_field]))
                    )
                    if not self._metadata_matches(metadata, filter):
                        continue

            yield self._build_checkpoint_tuple(hash_data, thread_id, checkpoint_ns, cp_id)
            count += 1

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """同步保存 checkpoint"""
        configurable = config["configurable"].copy()
        thread_id = configurable.pop("thread_id")
        checkpoint_ns = configurable.pop("checkpoint_ns")
        parent_checkpoint_id = configurable.pop("checkpoint_id", None)
        key = self._key(thread_id, checkpoint_ns)

        next_config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint["id"],
            }
        }

        # 分离原始类型和复杂类型
        cp_copy = checkpoint.copy()
        cp_copy["channel_values"] = cp_copy["channel_values"].copy()
        blob_values: dict[str, Any] = {}

        for k, v in checkpoint["channel_values"].items():
            if isinstance(v, _DeltaSnapshot):
                blob_values[k] = cp_copy["channel_values"].pop(k)
                cp_copy["channel_values"][k] = True
            elif v is None or isinstance(v, (str, int, float, bool)):
                pass
            else:
                blob_values[k] = cp_copy["channel_values"].pop(k)

        # 序列化
        cp_type_tag, cp_bytes = self.serde.dumps_typed(cp_copy)
        md_type_tag, md_bytes = self.serde.dumps_typed(
            get_serializable_checkpoint_metadata(config, metadata)
        )

        cp_id = checkpoint["id"]
        fields: dict[str, str] = {
            self._cp_field(cp_id): base64.b64encode(cp_bytes).decode("utf-8"),
            self._cp_type_field(cp_id): cp_type_tag,
            self._cp_parent_field(cp_id): parent_checkpoint_id or "",
            self._meta_field(cp_id): base64.b64encode(md_bytes).decode("utf-8"),
            self._meta_type_field(cp_id): md_type_tag,
        }

        # blob fields
        if blob_versions := {k: v for k, v in new_versions.items() if k in blob_values}:
            fields.update(self._dump_blobs(blob_values, blob_versions))

        # 更新 order（需要先读取当前 order）
        current_order_json = self.conn.hget(key, _ORDER_FIELD)
        current_order = json.loads(current_order_json) if current_order_json else []
        new_order = self._update_order(current_order, cp_id)
        fields[_ORDER_FIELD] = json.dumps(new_order)

        # 写入
        self.conn.hset(key, mapping=fields)
        if self.ttl > 0:
            self.conn.expire(key, self.ttl)

        logger.debug(
            f"保存 checkpoint: thread={thread_id}, ns={checkpoint_ns}, "
            f"id={cp_id}, blobs={len(blob_values)}, key={key}"
        )

        return next_config

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """同步保存中间写入"""
        if not writes:
            return

        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"].get("checkpoint_id", "")
        key = self._key(thread_id, checkpoint_ns)

        write_fields = self._dump_writes(checkpoint_id, task_id, task_path, writes)
        self.conn.hset(key, mapping=write_fields)
        if self.ttl > 0:
            self.conn.expire(key, self.ttl)

        logger.debug(
            f"保存 writes: thread={thread_id}, checkpoint={checkpoint_id}, "
            f"task={task_id}, count={len(writes)}"
        )

    def delete_thread(self, thread_id: str) -> None:
        """同步删除线程的所有数据（1 个 key，1 次 DEL）"""
        # 查找所有匹配前缀的 key
        pattern = f"{_KEY_PREFIX}{thread_id}:*"
        keys_to_delete = []
        cursor = 0
        while True:
            cursor, keys = self.conn.scan(cursor, match=pattern, count=100)
            keys_to_delete.extend(keys)
            if cursor == 0:
                break

        if keys_to_delete:
            self.conn.delete(*keys_to_delete)
            logger.info(f"删除线程 {thread_id} 的 {len(keys_to_delete)} 个 key")


# ═════════════════════════════════════════════
# AsyncRedisSaver —— 异步实现
# ═════════════════════════════════════════════
class AsyncRedisSaver(BaseRedisSaver):
    """异步 Redis Checkpointer

    对标 ``langgraph.checkpoint.postgres.AsyncPostgresSaver``

    用法::

        import redis.asyncio as aioredis
        conn = aioredis.Redis(host="localhost", port=6379, db=0, decode_responses=True)
        saver = AsyncRedisSaver(conn)
        await saver.setup()
    """

    def __init__(
        self,
        conn: Any,  # redis.asyncio.Redis
        *,
        serde: SerializerProtocol | None = None,
        ttl: int = 0,
    ) -> None:
        super().__init__(serde=serde)
        self.conn = conn
        self.lock = asyncio.Lock()
        self.ttl = ttl
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = None

    @classmethod
    async def from_conn_info(
        cls,
        host: str = "localhost",
        port: int = 6379,
        db: int = 0,
        password: str | None = None,
        *,
        ttl: int = 0,
        serde: SerializerProtocol | None = None,
    ) -> AsyncRedisSaver:
        """从连接信息创建 AsyncRedisSaver 实例"""
        import redis.asyncio as aioredis

        conn = aioredis.Redis(
            host=host,
            port=port,
            db=db,
            password=password or None,
            decode_responses=True,
        )
        await conn.ping()
        return cls(conn, ttl=ttl, serde=serde)

    async def setup(self) -> None:
        """异步初始化检查点存储"""
        await self.conn.ping()
        logger.info("Redis Checkpointer 异步初始化完成")

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """异步获取 checkpoint"""
        thread_id = config["configurable"]["thread_id"]
        checkpoint_id = get_checkpoint_id(config)
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        key = self._key(thread_id, checkpoint_ns)

        if checkpoint_id:
            cp_field = self._cp_field(checkpoint_id)
            if not await self.conn.hexists(key, cp_field):
                return None
            hash_data = await self.conn.hgetall(key)
            if not hash_data:
                return None
        else:
            hash_data = await self.conn.hgetall(key)
            if not hash_data:
                return None
            order = self._parse_order(hash_data)
            if not order:
                return None
            checkpoint_id = order[0]

        return self._build_checkpoint_tuple(hash_data, thread_id, checkpoint_ns, checkpoint_id)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        """异步列出 checkpoints"""
        if config is None:
            return

        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        key = self._key(thread_id, checkpoint_ns)

        hash_data = await self.conn.hgetall(key)
        if not hash_data:
            return

        order = self._parse_order(hash_data)
        if not order:
            return

        before_id = get_checkpoint_id(before) if before else None
        count = 0

        for cp_id in order:
            if before_id and cp_id >= before_id:
                continue
            if limit is not None and count >= limit:
                break

            if filter:
                meta_field = self._meta_field(cp_id)
                meta_type_field = self._meta_type_field(cp_id)
                if meta_field in hash_data:
                    md_type = hash_data.get(meta_type_field, "msgpack")
                    metadata = self.serde.loads_typed(
                        (md_type, base64.b64decode(hash_data[meta_field]))
                    )
                    if not self._metadata_matches(metadata, filter):
                        continue

            yield self._build_checkpoint_tuple(hash_data, thread_id, checkpoint_ns, cp_id)
            count += 1

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """异步保存 checkpoint"""
        configurable = config["configurable"].copy()
        thread_id = configurable.get("thread_id")
        checkpoint_ns = configurable.get("checkpoint_ns", "")
        parent_checkpoint_id = configurable.get("checkpoint_id", None)
        key = self._key(thread_id, checkpoint_ns)

        next_config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint["id"],
            }
        }

        # 分离原始类型和复杂类型
        cp_copy = checkpoint.copy()
        cp_copy["channel_values"] = cp_copy["channel_values"].copy()
        blob_values: dict[str, Any] = {}

        for k, v in checkpoint["channel_values"].items():
            if isinstance(v, _DeltaSnapshot):
                blob_values[k] = cp_copy["channel_values"].pop(k)
                cp_copy["channel_values"][k] = True
            elif v is None or isinstance(v, (str, int, float, bool)):
                pass
            else:
                blob_values[k] = cp_copy["channel_values"].pop(k)

        # 序列化
        cp_type_tag, cp_bytes = self.serde.dumps_typed(cp_copy)
        md_type_tag, md_bytes = self.serde.dumps_typed(
            get_serializable_checkpoint_metadata(config, metadata)
        )

        cp_id = checkpoint["id"]
        fields: dict[str, str] = {
            self._cp_field(cp_id): base64.b64encode(cp_bytes).decode("utf-8"),
            self._cp_type_field(cp_id): cp_type_tag,
            self._cp_parent_field(cp_id): parent_checkpoint_id or "",
            self._meta_field(cp_id): base64.b64encode(md_bytes).decode("utf-8"),
            self._meta_type_field(cp_id): md_type_tag,
        }

        # blob fields
        if blob_versions := {k: v for k, v in new_versions.items() if k in blob_values}:
            fields.update(self._dump_blobs(blob_values, blob_versions))

        # 更新 order
        current_order_json = await self.conn.hget(key, _ORDER_FIELD)
        current_order = json.loads(current_order_json) if current_order_json else []
        new_order = self._update_order(current_order, cp_id)
        fields[_ORDER_FIELD] = json.dumps(new_order)

        # 写入
        await self.conn.hset(key, mapping=fields)
        if self.ttl > 0:
            await self.conn.expire(key, self.ttl)

        logger.debug(
            f"保存 checkpoint: thread={thread_id}, ns={checkpoint_ns}, "
            f"id={cp_id}, blobs={len(blob_values)}, key={key}"
        )

        return next_config

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """异步保存中间写入"""
        if not writes:
            return

        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"].get("checkpoint_id", "")
        key = self._key(thread_id, checkpoint_ns)

        write_fields = self._dump_writes(checkpoint_id, task_id, task_path, writes)
        await self.conn.hset(key, mapping=write_fields)
        if self.ttl > 0:
            await self.conn.expire(key, self.ttl)

        logger.debug(
            f"保存 writes: thread={thread_id}, checkpoint={checkpoint_id}, "
            f"task={task_id}, count={len(writes)}"
        )

    async def adelete_thread(self, thread_id: str) -> None:
        """异步删除线程的所有数据"""
        pattern = f"{_KEY_PREFIX}{thread_id}:*"
        keys_to_delete: list[str] = []
        async for key in self.conn.scan_iter(match=pattern, count=100):
            keys_to_delete.append(key)

        if keys_to_delete:
            await self.conn.delete(*keys_to_delete)
            logger.info(f"删除线程 {thread_id} 的 {len(keys_to_delete)} 个 key")

    # ── 同步包装器（跨线程委托） ──────────────

    def _check_not_main_thread(self, method_name: str) -> None:
        if self.loop is None:
            return
        try:
            if asyncio.get_running_loop() is self.loop:
                raise asyncio.InvalidStateError(
                    f"AsyncRedisSaver 的同步方法 ({method_name}) 仅允许从不同线程调用。"
                    f"在主线程中请使用异步接口，例如 await checkpointer.a{method_name}(...)"
                )
        except RuntimeError:
            pass

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        self._check_not_main_thread("get_tuple")
        loop = self.loop or asyncio.get_event_loop()
        return asyncio.run_coroutine_threadsafe(self.aget_tuple(config), loop).result()

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        self._check_not_main_thread("list")
        loop = self.loop or asyncio.get_event_loop()
        aiter_ = self.alist(config, filter=filter, before=before, limit=limit)
        while True:
            try:
                yield asyncio.run_coroutine_threadsafe(
                    anext(aiter_),
                    loop,  # type: ignore[arg-type]
                ).result()
            except StopAsyncIteration:
                break

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        self._check_not_main_thread("put")
        loop = self.loop or asyncio.get_event_loop()
        return asyncio.run_coroutine_threadsafe(
            self.aput(config, checkpoint, metadata, new_versions), loop
        ).result()

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self._check_not_main_thread("put_writes")
        loop = self.loop or asyncio.get_event_loop()
        return asyncio.run_coroutine_threadsafe(
            self.aput_writes(config, writes, task_id, task_path), loop
        ).result()

    def delete_thread(self, thread_id: str) -> None:
        self._check_not_main_thread("delete_thread")
        loop = self.loop or asyncio.get_event_loop()
        return asyncio.run_coroutine_threadsafe(self.adelete_thread(thread_id), loop).result()


__all__ = [
    "BaseRedisSaver",
    "RedisSaver",
    "AsyncRedisSaver",
]
