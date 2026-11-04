"""用户认证模型

用于用户登录认证，存储用户账号信息。
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.entity.mysql_base import Base


class User(Base):
    """用户认证表

    存储用户登录信息，包括用户名、密码哈希、角色等。

    Attributes:
        id: 自增主键
        username: 登录用户名（唯一）
        hashed_password: bcrypt 哈希后的密码
        display_name: 显示名称
        is_active: 是否启用
        is_admin: 是否管理员
        created_at: 创建时间
        updated_at: 更新时间
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False, comment="登录用户名"
    )
    hashed_password: Mapped[str] = mapped_column(
        String(256), nullable=False, comment="bcrypt 哈希密码"
    )
    display_name: Mapped[str] = mapped_column(
        String(128), nullable=False, default="", comment="显示名称"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, comment="是否启用"
    )
    is_admin: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, comment="是否管理员"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), comment="更新时间"
    )

    def __repr__(self) -> str:
        return f"<User(id={self.id}, username={self.username}, is_admin={self.is_admin})>"
