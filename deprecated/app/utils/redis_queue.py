"""通用 Redis 队列

基于 Redis List 实现的轻量级消息队列，提供生产-消费模式。
队列本身不绑定任何业务逻辑，消费行为通过 handler 回调注入。

架构设计：
┌─────────────┐     ┌──────────────┐     ┌──────────────┐
│  生产者      │────▶│ Redis List   │────▶│ 消费者协程    │
│ enqueue()   │     │ (持久化队列)  │     │ (BLPOP 阻塞) │
└─────────────┘     └──────────────┘     └──────────────┘
       │                    │                     │
       │ 失败               │ 不可用              │ 失败
       ▼                    ▼                     ▼
┌─────────────┐     ┌──────────────┐     ┌──────────────┐
│ asyncio.Queue│     │ 降级到内存队列 │     │ 重试 N 次    │
│ (内存兜底)   │     │              │     │ 记录兜底日志  │
└─────────────┘     └──────────────┘     └──────────────┘

核心功能：
1. enqueue() — 生产者入队任意可序列化数据（优先 Redis，失败降级到内存）
2. start_consumer() — 启动后台消费者协程，通过 handler 回调处理消息
3. stop_consumer() — 优雅停止消费者
4. BLPOP 阻塞等待 — 零轮询，消息入队即刻唤醒消费者
5. 队列深度保护 — 超过阈值时拒绝入队，由调用方降级处理
6. 批量消费 + 重试机制
7. 失败消息记录到兜底日志文件
8. 消费监控指标 — 成功/失败计数、延迟统计
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from app import config


class RedisQueue:
    """通用 Redis 队列

    提供 Redis List 消息队列 + 内存队列兜底 + 后台消费者。
    队列不感知业务逻辑，消费行为由 handler 回调决定。

    消费者使用 BLPOP 阻塞等待，零轮询、零延迟。
    """

    def __init__(
        self,
        queue_key: str = "redis_queue:default",
        batch_size: int = 10,
        retry_count: int = 3,
        max_queue_size: int = 0,
        blpop_timeout: int = 5,
        fallback_log_path: str = f"{config.BASE_DIR}/logs/redis_queue_fallback.jsonl",
    ) -> None:
        """初始化队列

        Args:
            queue_key: Redis List key
            batch_size: 每批消费的消息数量
            retry_count: 消费失败重试次数
            max_queue_size: 队列最大深度，0 表示不限制
            blpop_timeout: BLPOP 阻塞超时（秒），0 表示永久阻塞
            fallback_log_path: 兜底日志文件路径
        """
        self._redis_client = None
        self._memory_queue: asyncio.Queue = asyncio.Queue()
        self._consumer_task: asyncio.Task | None = None
        self._running = False
        self._handler: Callable[[Any], Awaitable[None]] | None = None
        self._queue_key = queue_key
        self._batch_size = batch_size
        self._retry_count = retry_count
        self._max_queue_size = max_queue_size
        self._blpop_timeout = blpop_timeout
        self._fallback_log_path = Path(fallback_log_path)
        self._use_memory_fallback = False

        # BLPOP 专用连接（避免占用业务连接池）
        self._blpop_conn = None

        # 监控指标
        self._stats = {
            "enqueued": 0,
            "dequeued": 0,
            "consumed_success": 0,
            "consumed_failed": 0,
            "fallback_to_memory": 0,
            "rejected_queue_full": 0,
            "total_latency_ms": 0.0,
        }

        # 确保兜底日志目录存在
        self._fallback_log_path.parent.mkdir(parents=True, exist_ok=True)

    async def _get_redis_client(self):
        """获取 Redis 客户端（延迟初始化）"""
        if self._redis_client is None:
            from app.core.manager.redis_client import redis_manager

            self._redis_client = await redis_manager.get_client()
        return self._redis_client

    async def _get_blpop_connection(self):
        """获取 BLPOP 专用连接（独立于业务连接池）

        BLPOP 会长时间阻塞，必须使用独立连接避免影响其他 Redis 操作。
        """
        if self._blpop_conn is None:
            from app.core.manager.redis_client import redis_manager

            # 创建独立连接，不复用连接池
            self._blpop_conn = await redis_manager.get_client()
        return self._blpop_conn

    async def close_blpop_connection(self):
        """关闭 BLPOP 专用连接"""
        if self._blpop_conn is not None:
            try:
                await self._blpop_conn.aclose()
            except Exception:
                pass
            self._blpop_conn = None

    async def enqueue(self, data: Any) -> bool:
        """将数据入队

        优先使用 Redis List，失败时降级到内存队列。
        当队列深度超过 max_queue_size 时，返回 False 由调用方降级处理。

        Args:
            data: 任意可 JSON 序列化的数据

        Returns:
            bool: 是否成功入队（False 表示队列已满或全部失败，调用方应降级）
        """
        message_json = json.dumps(data, ensure_ascii=False, default=str)

        # 队列深度保护：超过阈值时拒绝入队
        if self._max_queue_size > 0 and not self._use_memory_fallback:
            try:
                redis_client = await self._get_redis_client()
                current_size = await redis_client.llen(self._queue_key)
                if current_size >= self._max_queue_size:
                    logger.warning(
                        f"队列深度已达上限 ({current_size}/{self._max_queue_size})，拒绝入队"
                    )
                    self._stats["rejected_queue_full"] += 1
                    return False
            except Exception as e:
                logger.warning(f"检查队列深度失败: {e}")

        # 尝试推入 Redis
        try:
            redis_client = await self._get_redis_client()
            await redis_client.lpush(self._queue_key, message_json)
            logger.debug(f"消息已入队 Redis: queue_key={self._queue_key}")
            self._use_memory_fallback = False
            self._stats["enqueued"] += 1
            return True
        except Exception as e:
            logger.warning(f"Redis 入队失败，降级到内存队列: {e}")
            self._use_memory_fallback = True

        # 降级到内存队列
        try:
            self._memory_queue.put_nowait(message_json)
            logger.info("消息已入队内存队列")
            self._stats["enqueued"] += 1
            self._stats["fallback_to_memory"] += 1
            return True
        except Exception as e:
            logger.error(f"内存队列入队失败: {e}")
            self._log_fallback_message(message_json, str(e))
            return False

    async def start_consumer(
        self, handler: Callable[[Any], Awaitable[None]]
    ) -> None:
        """启动后台消费者协程

        Args:
            handler: 异步回调函数，接收反序列化后的数据并处理
        """
        if self._running:
            logger.warning("消费者已在运行")
            return

        self._handler = handler
        self._running = True
        self._consumer_task = asyncio.create_task(self._consume_loop())
        logger.info(
            f"消费者已启动: queue_key={self._queue_key}, "
            f"batch_size={self._batch_size}, blpop_timeout={self._blpop_timeout}s"
        )

    async def stop_consumer(self) -> None:
        """优雅停止消费者

        等待当前批次处理完成，并处理剩余消息。
        """
        if not self._running:
            return

        logger.info("正在停止消费者...")
        self._running = False

        if self._consumer_task:
            try:
                await asyncio.wait_for(self._consumer_task, timeout=10.0)
            except TimeoutError:
                logger.warning("消费者停止超时，强制取消")
                self._consumer_task.cancel()
            self._consumer_task = None

        # 处理剩余消息
        await self._drain_remaining_messages()
        self._handler = None

        # 关闭 BLPOP 专用连接
        await self.close_blpop_connection()

        logger.info(f"消费者已停止, 统计: {self._stats}")

    async def _consume_loop(self) -> None:
        """消费者主循环（BLPOP 阻塞模式）

        使用 BLPOP 阻塞等待消息，零轮询、零延迟。
        收到第一条消息后，立即用 pipeline RPOP 批量取出剩余消息。
        """
        logger.info("消费者循环开始运行（BLPOP 模式）")

        while self._running:
            try:
                processed = await self._process_with_blpop()
                if processed == 0:
                    # 尝试从内存队列消费（内存队列无 BLPOP，直接检查）
                    processed = await self._process_memory_batch()
                    if processed == 0:
                        # 无消息，短暂 sleep 避免空转（仅影响内存队列路径）
                        await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"消费者循环异常: {e}")
                await asyncio.sleep(2.0)

        logger.info("消费者循环结束")

    async def _process_with_blpop(self) -> int:
        """使用 BLPOP 阻塞等待并处理消息

        Returns:
            int: 成功处理的消息数量
        """
        # 优先从 Redis BLPOP
        if not self._use_memory_fallback:
            try:
                return await self._blpop_and_batch()
            except Exception as e:
                logger.warning(f"Redis BLPOP 失败，切换到内存队列: {e}")
                self._use_memory_fallback = True

        # 从内存队列消费
        return await self._process_memory_batch()

    async def _blpop_and_batch(self) -> int:
        """BLPOP 等待第一条消息，然后 pipeline RPOP 批量取出剩余消息

        Returns:
            int: 成功处理的消息数量
        """
        redis_client = await self._get_blpop_connection()

        # BLPOP 阻塞等待第一条消息
        result = await redis_client.blpop(self._queue_key, timeout=self._blpop_timeout)
        if result is None:
            # 超时，无消息
            return 0

        # result 格式: (queue_key_bytes, value_bytes)
        _, first_msg = result
        self._stats["dequeued"] += 1

        # 立即用 pipeline RPOP 批量取出剩余消息（非阻塞）
        remaining = []
        if self._batch_size > 1:
            try:
                # 使用业务连接做 pipeline RPOP（BLPOP 连接已被阻塞占用）
                business_client = await self._get_redis_client()
                pipe = business_client.pipeline()
                for _ in range(self._batch_size - 1):
                    pipe.rpop(self._queue_key)
                results = await pipe.execute()
                remaining = [r for r in results if r is not None]
                self._stats["dequeued"] += len(remaining)
            except Exception as e:
                logger.warning(f"批量 RPOP 失败（仅处理第一条）: {e}")

        # 合并所有消息
        all_messages = [first_msg] + remaining

        # 处理消息
        success_count = 0
        failed_messages = []

        for msg_json in all_messages:
            try:
                data = json.loads(msg_json)
                # 计算消费延迟
                enqueue_ts = data.get("_enqueue_ts")
                if enqueue_ts and self._handler:
                    latency_ms = (time.time() - enqueue_ts) * 1000
                    self._stats["total_latency_ms"] += latency_ms

                if self._handler:
                    await self._handler(data)
                success_count += 1
                self._stats["consumed_success"] += 1
            except Exception as e:
                logger.error(f"消息处理失败: {e}")
                failed_messages.append(msg_json)
                self._stats["consumed_failed"] += 1

        # 失败消息重试
        for msg_json in failed_messages:
            await self._retry_failed_message(msg_json)

        if success_count > 0:
            logger.debug(f"本批次处理 {success_count} 条消息")

        return success_count

    async def _process_batch(self) -> int:
        """处理一批消息（兼容旧调用）

        Returns:
            int: 成功处理的消息数量
        """
        # 优先从 Redis 消费
        if not self._use_memory_fallback:
            try:
                processed = await self._process_redis_batch()
                if processed > 0:
                    return processed
            except Exception as e:
                logger.warning(f"Redis 消费失败，切换到内存队列: {e}")
                self._use_memory_fallback = True

        # 从内存队列消费
        return await self._process_memory_batch()

    async def _process_redis_batch(self) -> int:
        """从 Redis 消费一批消息（pipeline RPOP，非阻塞）"""
        redis_client = await self._get_redis_client()

        pipe = redis_client.pipeline()
        for _ in range(self._batch_size):
            pipe.rpop(self._queue_key)
        results = await pipe.execute()

        messages = [r for r in results if r is not None]
        if not messages:
            return 0

        self._stats["dequeued"] += len(messages)

        success_count = 0
        failed_messages = []

        for msg_json in messages:
            try:
                data = json.loads(msg_json)
                if self._handler:
                    await self._handler(data)
                success_count += 1
                self._stats["consumed_success"] += 1
            except Exception as e:
                logger.error(f"消息处理失败: {e}")
                failed_messages.append(msg_json)
                self._stats["consumed_failed"] += 1

        # 失败消息重试
        for msg_json in failed_messages:
            await self._retry_failed_message(msg_json)

        return success_count

    async def _process_memory_batch(self) -> int:
        """从内存队列消费一批消息"""
        processed = 0

        while processed < self._batch_size and not self._memory_queue.empty():
            try:
                msg_json = self._memory_queue.get_nowait()
                data = json.loads(msg_json)
                if self._handler:
                    await self._handler(data)
                processed += 1
                self._memory_queue.task_done()
                self._stats["consumed_success"] += 1
            except asyncio.QueueEmpty:
                break
            except Exception as e:
                logger.error(f"内存队列消息处理失败: {e}")
                self._log_fallback_message(msg_json, str(e))
                self._memory_queue.task_done()
                self._stats["consumed_failed"] += 1

        return processed

    async def _retry_failed_message(self, msg_json: str) -> None:
        """重试失败的消息"""
        for attempt in range(1, self._retry_count + 1):
            try:
                data = json.loads(msg_json)
                if self._handler:
                    await self._handler(data)
                logger.info(f"消息重试成功 (attempt {attempt})")
                self._stats["consumed_success"] += 1
                return
            except Exception as e:
                logger.warning(
                    f"消息重试失败 (attempt {attempt}/{self._retry_count}): {e}"
                )
                if attempt < self._retry_count:
                    await asyncio.sleep(1.0)

        logger.error("消息重试全部失败，记录到兜底日志")
        self._log_fallback_message(msg_json, "所有重试失败")
        self._stats["consumed_failed"] += 1

    def _log_fallback_message(self, msg_json: str, reason: str) -> None:
        """记录失败消息到兜底日志文件"""
        try:
            log_entry = {
                "timestamp": datetime.now().isoformat(),
                "reason": reason,
                "message": msg_json,
            }
            with open(self._fallback_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
            logger.warning(f"失败消息已记录到兜底日志: {self._fallback_log_path}")
        except Exception as e:
            logger.error(f"记录兜底日志失败: {e}")

    async def _drain_remaining_messages(self) -> None:
        """处理剩余消息（关闭前调用）"""
        logger.info("处理剩余消息...")

        try:
            while True:
                processed = await self._process_redis_batch()
                if processed == 0:
                    break
        except Exception as e:
            logger.error(f"处理 Redis 剩余消息失败: {e}")

        try:
            while not self._memory_queue.empty():
                await self._process_memory_batch()
        except Exception as e:
            logger.error(f"处理内存队列剩余消息失败: {e}")

        logger.info("剩余消息处理完成")

    async def get_queue_size(self) -> dict:
        """获取队列大小

        Returns:
            dict: {"redis": int, "memory": int, "total": int}
        """
        redis_size = 0
        memory_size = self._memory_queue.qsize()

        if not self._use_memory_fallback:
            try:
                redis_client = await self._get_redis_client()
                redis_size = await redis_client.llen(self._queue_key)
            except Exception:
                pass

        return {
            "redis": redis_size,
            "memory": memory_size,
            "total": redis_size + memory_size,
        }

    def get_stats(self) -> dict:
        """获取消费者统计指标

        Returns:
            dict: 包含入队/出队/成功/失败/延迟等统计数据
        """
        avg_latency = 0.0
        total_consumed = self._stats["consumed_success"] + self._stats["consumed_failed"]
        if self._stats["consumed_success"] > 0:
            avg_latency = self._stats["total_latency_ms"] / self._stats["consumed_success"]

        return {
            **self._stats,
            "avg_latency_ms": round(avg_latency, 2),
            "total_consumed": total_consumed,
        }

