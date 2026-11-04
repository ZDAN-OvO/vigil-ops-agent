"""认证请求/响应模型

定义用户认证相关的 Pydantic 模型。
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    """登录请求"""

    username: str = Field(..., min_length=1, max_length=64, description="用户名")
    password: str = Field(..., min_length=1, description="密码")


class LoginResponse(BaseModel):
    """登录响应"""

    access_token: str = Field(..., description="访问令牌")
    refresh_token: str = Field(..., description="刷新令牌")
    token_type: str = Field(default="bearer", description="令牌类型")
    user: "UserInfoResponse" = Field(..., description="用户信息")


class RefreshRequest(BaseModel):
    """刷新令牌请求"""

    refresh_token: str = Field(..., description="刷新令牌")


class TokenResponse(BaseModel):
    """令牌响应"""

    access_token: str = Field(..., description="访问令牌")
    token_type: str = Field(default="bearer", description="令牌类型")


class UserCreateRequest(BaseModel):
    """创建用户请求（管理员）"""

    username: str = Field(..., min_length=3, max_length=64, description="用户名")
    password: str = Field(..., min_length=8, max_length=128, description="密码（至少8位）")
    display_name: str = Field(default="", max_length=128, description="显示名称")
    is_admin: bool = Field(default=False, description="是否管理员")


class UserUpdateRequest(BaseModel):
    """更新用户请求（管理员）"""

    display_name: Optional[str] = Field(None, max_length=128, description="显示名称")
    is_active: Optional[bool] = Field(None, description="是否启用")
    is_admin: Optional[bool] = Field(None, description="是否管理员")
    password: Optional[str] = Field(None, min_length=8, max_length=128, description="新密码")


class UserInfoResponse(BaseModel):
    """用户信息响应"""

    id: int = Field(..., description="用户 ID")
    username: str = Field(..., description="用户名")
    display_name: str = Field(..., description="显示名称")
    is_active: bool = Field(..., description="是否启用")
    is_admin: bool = Field(..., description="是否管理员")
    created_at: datetime = Field(..., description="创建时间")

    class Config:
        from_attributes = True


class UserListResponse(BaseModel):
    """用户列表响应"""

    users: list[UserInfoResponse] = Field(..., description="用户列表")
    total: int = Field(..., description="总数")


# 解决前向引用
LoginResponse.model_rebuild()
