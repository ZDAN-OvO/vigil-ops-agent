"""会话管理模型

用于管理用户的对话会话，记录会话元数据。
用户可以查询自己拥有的会话列表，实现会话的生命周期管理。

设计思路：
- session_id 由后端生成（UUID），确保唯一性和安全性
- user_id 关联 users.id（INT 类型）
- 支持软删除（is_deleted 标记）
- 冗余 message_count 便于排序和展示
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, func, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.models.entity.mysql_base import Base


class ConversationSession(Base):
    """会话表

    管理用户的对话会话，用于：
    1. 查询用户拥有的会话列表
    2. 存储会话标题和元数据
    3. 支持会话的软删除

    Attributes:
        id: 自增主键
        session_id: 会话 ID（唯一，由后端生成 UUID）
        user_id: 用户 ID（关联 users.id）
        title: 会话标题（可从第一条消息自动生成）
        message_count: 消息数量（冗余字段，便于排序）
        created_at: 创建时间
        updated_at: 最后更新时间
        is_deleted: 是否已删除（软删除标记）
    """

    __tablename__ = "conversation_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, comment="会话ID（唯一）"
    )
    user_id: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="用户ID（关联 users.id）"
    )
    title: Mapped[str] = mapped_column(
        String(256), nullable=False, default="", comment="会话标题"
    )
    message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="消息数量（冗余）"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), comment="最后更新时间"
    )
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="是否已删除（软删除）"
    )

    # 索引：加速按用户查询和排序
    __table_args__ = (
        Index("idx_user_id", "user_id"),
        Index("idx_user_updated", "user_id", "updated_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<ConversationSession(session_id={self.session_id}, "
            f"user_id={self.user_id}, title={self.title})>"
        )
