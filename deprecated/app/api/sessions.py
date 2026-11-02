"""会话管理接口

提供会话列表查询、创建、更新、删除等接口
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger

from app.core.auth_resolver import get_current_user
from app.models.dto.chat_request import CreateSessionRequest, UpdateSessionRequest
from app.models.dto.response import (
    SessionListResponse,
    SessionResponse,
    ApiResponse,
)
from app.models.entity.user import User
from app.services.conversation_session_service import conversation_session_service

router = APIRouter()


@router.get("/", response_model=SessionListResponse)
async def list_sessions(
    offset: int = Query(0, ge=0, description="偏移量"),
    limit: int = Query(20, ge=1, le=100, description="每页数量"),
    current_user: User = Depends(get_current_user),
) -> SessionListResponse:
    """获取当前用户的会话列表

    Args:
        offset: 偏移量
        limit: 每页数量
        current_user: 当前用户

    Returns:
        SessionListResponse: 会话列表
    """
    try:
        sessions, total = await conversation_session_service.list_sessions(
            user_id=current_user.id,
            offset=offset,
            limit=limit,
        )

        return SessionListResponse(
            sessions=[
                SessionResponse(
                    session_id=s.session_id,
                    title=s.title,
                    message_count=s.message_count,
                    created_at=s.created_at.isoformat() if s.created_at else None,
                    updated_at=s.updated_at.isoformat() if s.updated_at else None,
                )
                for s in sessions
            ],
            total=total,
            offset=offset,
            limit=limit,
        )

    except Exception as e:
        logger.error(f"获取会话列表失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/", response_model=SessionResponse)
async def create_session(
    request: CreateSessionRequest,
    current_user: User = Depends(get_current_user),
) -> SessionResponse:
    """创建新会话

    Args:
        request: 创建请求
        current_user: 当前用户

    Returns:
        SessionResponse: 创建的会话
    """
    try:
        # session_id 由后端自动生成
        session = await conversation_session_service.create_session(
            user_id=current_user.id,
            title=request.title or "",
        )

        return SessionResponse(
            session_id=session.session_id,
            title=session.title,
            message_count=session.message_count,
            created_at=session.created_at.isoformat() if session.created_at else None,
            updated_at=session.updated_at.isoformat() if session.updated_at else None,
        )

    except Exception as e:
        logger.error(f"创建会话失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
) -> SessionResponse:
    """获取单个会话

    Args:
        session_id: 会话 ID
        current_user: 当前用户

    Returns:
        SessionResponse: 会话信息
    """
    try:
        session = await conversation_session_service.get_session(
            session_id=session_id,
            user_id=current_user.id,
        )

        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在")

        return SessionResponse(
            session_id=session.session_id,
            title=session.title,
            message_count=session.message_count,
            created_at=session.created_at.isoformat() if session.created_at else None,
            updated_at=session.updated_at.isoformat() if session.updated_at else None,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取会话失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{session_id}", response_model=SessionResponse)
async def update_session(
    session_id: str,
    request: UpdateSessionRequest,
    current_user: User = Depends(get_current_user),
) -> SessionResponse:
    """更新会话

    Args:
        session_id: 会话 ID
        request: 更新请求
        current_user: 当前用户

    Returns:
        SessionResponse: 更新后的会话
    """
    try:
        session = await conversation_session_service.update_session(
            session_id=session_id,
            user_id=current_user.id,
            title=request.title,
        )

        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在")

        return SessionResponse(
            session_id=session.session_id,
            title=session.title,
            message_count=session.message_count,
            created_at=session.created_at.isoformat() if session.created_at else None,
            updated_at=session.updated_at.isoformat() if session.updated_at else None,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"更新会话失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{session_id}", response_model=ApiResponse)
async def delete_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
) -> ApiResponse:
    """删除会话（软删除）

    Args:
        session_id: 会话 ID
        current_user: 当前用户

    Returns:
        ApiResponse: 操作结果
    """
    try:
        success = await conversation_session_service.delete_session(
            session_id=session_id,
            user_id=current_user.id,
        )

        return ApiResponse(
            status="success" if success else "error",
            message="会话已删除" if success else "会话不存在或删除失败",
            data=None,
        )

    except Exception as e:
        logger.error(f"删除会话失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))
