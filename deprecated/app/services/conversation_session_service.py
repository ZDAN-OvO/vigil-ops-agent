"""会话管理服务

负责管理用户的对话会话，提供会话的 CRUD 操作。

核心功能：
1. create_session() - 创建新会话（自动生成 session_id）
2. get_session() - 获取单个会话
3. list_sessions() - 获取用户的会话列表
4. update_session() - 更新会话标题
5. delete_session() - 删除会话（软删除）
6. increment_message_count() - 增加消息计数
7. ensure_session_exists() - 确保会话存在（不存在则创建）
"""

import uuid
from typing import Optional

from loguru import logger
from sqlalchemy import select, update, func

from app.core.manager.mysql_client import mysql_manager
from app.models.entity.conversation_session import ConversationSession


class ConversationSessionService:
    """会话管理服务

    提供会话的 CRUD 操作和生命周期管理。
    """

    async def init_db(self) -> None:
        """初始化数据库表（创建 conversation_sessions 表）"""
        from app.models.entity.mysql_base import Base

        engine = await mysql_manager.get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("会话管理数据库表初始化完成")

    async def create_session(
        self,
        user_id: int,
        session_id: Optional[str] = None,
        title: str = "",
    ) -> ConversationSession:
        """创建新会话

        Args:
            user_id: 用户 ID
            session_id: 会话 ID（可选，为空时自动生成 UUID）
            title: 会话标题（可选）

        Returns:
            ConversationSession: 创建的会话对象
        """
        try:
            # 如果 session_id 为空，自动生成 UUID
            if not session_id:
                session_id = str(uuid.uuid4())
                logger.info(f"自动生成 session_id: {session_id}")

            async with mysql_manager.get_session() as session:
                # 检查是否已存在
                result = await session.execute(
                    select(ConversationSession).where(ConversationSession.session_id == session_id)
                )
                existing = result.scalar_one_or_none()
                if existing:
                    session.expunge(existing)
                    logger.debug(f"会话已存在: {session_id}")
                    return existing

                # 创建新会话
                new_session = ConversationSession(
                    session_id=session_id,
                    user_id=user_id,
                    title=title or f"会话 {session_id[:8]}",
                    message_count=0,
                )
                session.add(new_session)
                await session.flush()
                await session.refresh(new_session)
                session.expunge(new_session)

                logger.info(f"创建新会话: session_id={session_id}, user_id={user_id}")
                return new_session

        except Exception as e:
            logger.error(f"创建会话失败: session_id={session_id}, 错误: {e}")
            raise

    async def get_session(
        self,
        session_id: str,
        user_id: Optional[int] = None,
    ) -> Optional[ConversationSession]:
        """获取单个会话

        Args:
            session_id: 会话 ID
            user_id: 用户 ID（可选，用于权限校验）

        Returns:
            ConversationSession 或 None
        """
        try:
            async with mysql_manager.get_session() as session:
                query = select(ConversationSession).where(
                    ConversationSession.session_id == session_id,
                    ConversationSession.is_deleted == False,  # noqa: E712
                )
                if user_id is not None:
                    query = query.where(ConversationSession.user_id == user_id)

                result = await session.execute(query)
                obj = result.scalar_one_or_none()
                if obj is not None:
                    session.expunge(obj)
                return obj

        except Exception as e:
            logger.error(f"获取会话失败: session_id={session_id}, 错误: {e}")
            return None

    async def list_sessions(
        self,
        user_id: int,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[ConversationSession], int]:
        """获取用户的会话列表

        Args:
            user_id: 用户 ID
            offset: 偏移量
            limit: 每页数量

        Returns:
            tuple: (会话列表, 总数)
        """
        try:
            async with mysql_manager.get_session() as session:
                # 获取总数
                count_result = await session.execute(
                    select(func.count())
                    .select_from(ConversationSession)
                    .where(
                        ConversationSession.user_id == user_id,
                        ConversationSession.is_deleted == False,  # noqa: E712
                    )
                )
                total = count_result.scalar() or 0

                # 获取会话列表（按更新时间倒序）
                result = await session.execute(
                    select(ConversationSession)
                    .where(
                        ConversationSession.user_id == user_id,
                        ConversationSession.is_deleted == False,  # noqa: E712
                    )
                    .order_by(ConversationSession.updated_at.desc())
                    .offset(offset)
                    .limit(limit)
                )
                sessions = list(result.scalars().all())
                for s in sessions:
                    session.expunge(s)

                return sessions, total

        except Exception as e:
            logger.error(f"获取会话列表失败: user_id={user_id}, 错误: {e}")
            return [], 0

    async def update_session(
        self,
        session_id: str,
        user_id: int,
        title: Optional[str] = None,
    ) -> Optional[ConversationSession]:
        """更新会话

        Args:
            session_id: 会话 ID
            user_id: 用户 ID（用于权限校验）
            title: 新标题（可选）

        Returns:
            更新后的会话对象或 None
        """
        try:
            async with mysql_manager.get_session() as session:
                # 查找会话
                result = await session.execute(
                    select(ConversationSession).where(
                        ConversationSession.session_id == session_id,
                        ConversationSession.user_id == user_id,
                        ConversationSession.is_deleted == False,  # noqa: E712
                    )
                )
                conv_session = result.scalar_one_or_none()

                if conv_session is None:
                    return None

                # 更新字段
                if title is not None:
                    conv_session.title = title

                await session.flush()
                await session.refresh(conv_session)
                session.expunge(conv_session)
                logger.info(f"更新会话: session_id={session_id}")
                return conv_session

        except Exception as e:
            logger.error(f"更新会话失败: session_id={session_id}, 错误: {e}")
            return None

    async def delete_session(
        self,
        session_id: str,
        user_id: int,
    ) -> bool:
        """删除会话（软删除）

        Args:
            session_id: 会话 ID
            user_id: 用户 ID（用于权限校验）

        Returns:
            bool: 是否成功
        """
        try:
            async with mysql_manager.get_session() as session:
                # 软删除：标记 is_deleted = True
                result = await session.execute(
                    update(ConversationSession)
                    .where(
                        ConversationSession.session_id == session_id,
                        ConversationSession.user_id == user_id,
                        ConversationSession.is_deleted == False,  # noqa: E712
                    )
                    .values(is_deleted=True)
                )
                await session.flush()

                if result.rowcount > 0:
                    logger.info(f"删除会话: session_id={session_id}")
                    return True
                return False

        except Exception as e:
            logger.error(f"删除会话失败: session_id={session_id}, 错误: {e}")
            return False

    async def increment_message_count(
        self,
        session_id: str,
        count: int = 1,
    ) -> bool:
        """增加消息计数

        Args:
            session_id: 会话 ID
            count: 增加的数量

        Returns:
            bool: 是否成功
        """
        try:
            async with mysql_manager.get_session() as session:
                result = await session.execute(
                    update(ConversationSession)
                    .where(
                        ConversationSession.session_id == session_id,
                        ConversationSession.is_deleted == False,  # noqa: E712
                    )
                    .values(message_count=ConversationSession.message_count + count)
                )
                await session.flush()
                return result.rowcount > 0

        except Exception as e:
            logger.error(f"增加消息计数失败: session_id={session_id}, 错误: {e}")
            return False

    async def ensure_session_exists(
        self,
        user_id: int,
        session_id: Optional[str] = None,
        title: str = "",
    ) -> ConversationSession:
        """确保会话存在（不存在则创建）

        用于在对话时自动创建会话记录。

        Args:
            user_id: 用户 ID
            session_id: 会话 ID（可选，为空时自动生成）
            title: 会话标题（可选）

        Returns:
            ConversationSession: 会话对象
        """
        # 如果 session_id 为空，直接创建新会话
        if not session_id:
            return await self.create_session(user_id=user_id, title=title)

        # 先尝试获取
        existing = await self.get_session(session_id, user_id)
        if existing:
            return existing

        # 不存在则创建
        return await self.create_session(user_id=user_id, session_id=session_id, title=title)


# 全局单例
conversation_session_service = ConversationSessionService()
