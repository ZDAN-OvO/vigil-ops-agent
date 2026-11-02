"""
Redis 客户端工厂模块

提供异步 Redis 连接管理，用于存储会话 checkpoint 等短期记忆数据。
"""

from typing import Optional

import redis.asyncio as aioredis
from loguru import logger

from app.config import config


class RedisClientManager:
    """Redis 客户端管理器（异步）"""

    def __init__(self) -> None:
        self._client: Optional[aioredis.Redis] = None

    async def connect(self) -> aioredis.Redis:
        """
        连接到 Redis 服务器

        Returns:
            aioredis.Redis: 异步 Redis 客户端实例

        Raises:
            RuntimeError: 连接失败时抛出
        """
        if self._client is not None:
            logger.debug("Redis 已连接，跳过重复 connect")
            return self._client

        try:
            logger.info(f"正在连接到 Redis: {config.redis_host}:{config.redis_port}")

            self._client = aioredis.Redis(
                host=config.redis_host,
                port=config.redis_port,
                db=config.redis_db,
                password=config.redis_password or None,
                decode_responses=True,
                socket_connect_timeout=5,
            )

            # 验证连接
            await self._client.ping()
            logger.info("成功连接到 Redis")

            return self._client

        except Exception as e:
            logger.error(f"连接 Redis 失败: {e}")
            self._client = None
            raise RuntimeError(f"连接 Redis 失败: {e}") from e

    async def get_client(self) -> aioredis.Redis:
        """
        获取 Redis 客户端实例（自动连接）

        Returns:
            aioredis.Redis: 异步 Redis 客户端实例
        """
        if self._client is None:
            await self.connect()
        return self._client  # type: ignore[return-value]

    async def health_check(self) -> bool:
        """
        健康检查

        Returns:
            bool: True 表示健康，False 表示异常
        """
        try:
            client = await self.get_client()
            await client.ping()
            return True
        except Exception as e:
            logger.error(f"Redis 健康检查失败: {e}")
            return False

    async def close(self) -> None:
        """关闭连接"""
        if self._client is not None:
            try:
                await self._client.aclose()
                logger.info("已关闭 Redis 连接")
            except Exception as e:
                logger.error(f"关闭 Redis 连接失败: {e}")
            finally:
                self._client = None


# 全局单例
redis_manager = RedisClientManager()

if __name__ == "__main__":
    import asyncio

    try:
        asyncio.run(redis_manager.connect())
    except Exception as ex:
        print(ex)
