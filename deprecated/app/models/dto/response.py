"""响应数据模型

定义 API 响应的 Pydantic 模型
"""

from typing import List, Dict, Any, Optional

from pydantic import BaseModel, Field


class ChatResponse(BaseModel):
    """对话响应"""

    answer: str = Field(..., description="AI 回答")
    session_id: str = Field(..., description="会话 ID")


class SessionInfoResponse(BaseModel):
    """会话信息响应"""

    session_id: str = Field(..., description="会话 ID")
    message_count: int = Field(..., description="消息数量")
    history: List[Dict[str, str]] = Field(..., description="历史消息列表")


class ApiResponse(BaseModel):
    """通用 API 响应"""

    status: str = Field(..., description="状态")
    message: str = Field(..., description="消息")
    data: Optional[Any] = Field(None, description="数据")


class HealthResponse(BaseModel):
    """健康检查响应"""

    status: str = Field(..., description="状态")
    service: str = Field(..., description="服务名称")
    version: str = Field(..., description="版本号")


# ── 会话管理相关响应 ──────────────────────────


class SessionResponse(BaseModel):
    """单个会话响应"""

    session_id: str = Field(..., description="会话 ID")
    title: str = Field(..., description="会话标题")
    message_count: int = Field(..., description="消息数量")
    created_at: Optional[str] = Field(None, description="创建时间")
    updated_at: Optional[str] = Field(None, description="最后更新时间")


class SessionListResponse(BaseModel):
    """会话列表响应"""

    sessions: List[SessionResponse] = Field(..., description="会话列表")
    total: int = Field(..., description="总数")
    offset: int = Field(..., description="偏移量")
    limit: int = Field(..., description="每页数量")
