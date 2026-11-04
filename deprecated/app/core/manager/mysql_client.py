"""
MySQL 客户端工厂模块

提供异步 MySQL 连接管理，使用 SQLAlchemy AsyncEngine。
用于存储长期记忆数据，如用户画像。
"""

from contextlib import asynccontextmanager
from typing import Optional, AsyncGenerator

from loguru import logger
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import config


class MySQLClientManager:
    """MySQL 客户端管理器（异步）"""

    def __init__(self) -> None:
        self._engine: Optional[AsyncEngine] = None
        self._session_factory: Optional[async_sessionmaker[AsyncSession]] = None

    async def connect(self) -> AsyncEngine:
        """
        连接到 MySQL 服务器

        Returns:
            AsyncEngine: SQLAlchemy 异步引擎实例

        Raises:
            RuntimeError: 连接失败时抛出
        """
        if self._engine is not None:
            logger.debug("MySQL 已连接，跳过重复 connect")
            return self._engine

        try:
            # 构建异步连接 URL
            # aiomysql 异步驱动
            db_url = (
                f"mysql+aiomysql://{config.mysql_user}:{config.mysql_password}"
                f"@{config.mysql_host}:{config.mysql_port}/{config.mysql_db}"
                f"?charset=utf8mb4"
            )

            logger.info(
                f"正在连接到 MySQL: {config.mysql_host}:{config.mysql_port}/{config.mysql_db}"
            )

            self._engine = create_async_engine(
                db_url,
                echo=config.debug,
                pool_size=10,
                max_overflow=20,
                pool_pre_ping=True,  # 连接前自动 ping 检测
                pool_recycle=3600,  # 1 小时回收连接，避免 MySQL 8 小时超时
            )

            self._session_factory = async_sessionmaker(
                self._engine,
                class_=AsyncSession,
                expire_on_commit=False,
            )

            # 验证连接
            async with self._engine.connect() as conn:
                await conn.execute(__import__("sqlalchemy").text("SELECT 1"))

            logger.info("成功连接到 MySQL")
            return self._engine

        except Exception as e:
            logger.error(f"连接 MySQL 失败: {e}")
            self._engine = None
            self._session_factory = None
            raise RuntimeError(f"连接 MySQL 失败: {e}") from e

    async def get_engine(self) -> AsyncEngine:
        """
        获取 SQLAlchemy 异步引擎（自动连接）

        Returns:
            AsyncEngine: 异步引擎实例
        """
        if self._engine is None:
            await self.connect()
        return self._engine  # type: ignore[return-value]

    @asynccontextmanager
    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        """
        获取异步数据库会话（上下文管理器）

        Yields:
            AsyncSession: 异步数据库会话

        Example:
            async with mysql_manager.get_session() as session:
                result = await session.execute(select(User))
        """
        if self._session_factory is None:
            await self.connect()

        async with self._session_factory() as session:  # type: ignore[union-attr]
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def health_check(self) -> bool:
        """
        健康检查

        Returns:
            bool: True 表示健康，False 表示异常
        """
        try:
            engine = await self.get_engine()
            async with engine.connect() as conn:
                import sqlalchemy

                await conn.execute(sqlalchemy.text("SELECT 1"))
            return True
        except Exception as e:
            logger.error(f"MySQL 健康检查失败: {e}")
            return False

    async def close(self) -> None:
        """关闭连接"""
        if self._engine is not None:
            try:
                await self._engine.dispose()
                logger.info("已关闭 MySQL 连接")
            except Exception as e:
                logger.error(f"关闭 MySQL 连接失败: {e}")
            finally:
                self._engine = None
                self._session_factory = None


# 全局单例
mysql_manager = MySQLClientManager()

if __name__ == "__main__":
    import asyncio


    async def test_manager():
        await mysql_manager.connect()


    try:
        asyncio.run(test_manager())
    except Exception as ex:
        print(ex)
