"""对话接口

提供基于 RAG Agent 的普通对话和流式对话接口
"""

import json

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from sse_starlette.sse import EventSourceResponse

from app.core.auth_resolver import get_current_user
from app.models.dto.chat_request import ChatRequest, ClearRequest
from app.models.dto.response import ApiResponse, SessionInfoResponse
from app.models.entity.user import User
from app.services.conversation_session_service import conversation_session_service
from app.services.rag_agent_service import rag_agent_service

router = APIRouter()


@router.post("/completion")
async def chat(request: ChatRequest, current_user: User = Depends(get_current_user)):
    """快速对话接口
    {
        "code": 200,
        "message": "success",
        "data": {
            "success": true,
            "answer": "回答内容",
            "session_id": "会话 ID",
            "errorMessage": null
        }
    }

    Args:
        request: 对话请求

    Returns:
        统一格式的对话响应
    """
    try:
        # 如果 session_id 为空，创建新会话
        if not request.session_id:
            session = await conversation_session_service.create_session(
                user_id=current_user.id,
                title=request.question[:50] if len(request.question) > 50 else request.question,
            )
            session_id = session.session_id
            logger.info(f"创建新会话: {session_id}")
        else:
            session_id = request.session_id
            # 验证会话归属
            existing_session = await conversation_session_service.get_session(
                session_id=session_id,
                user_id=current_user.id,
            )
            if not existing_session:
                return {
                    "code": 403,
                    "message": "error",
                    "data": {
                        "success": False,
                        "answer": None,
                        "session_id": None,
                        "errorMessage": "会话不存在或无权访问",
                    },
                }

        logger.info(f"[会话 {session_id}] 收到快速对话请求: {request.question}")

        result = await rag_agent_service.query(
            request.question,
            session_id=session_id,
            user_id=str(current_user.id),
        )

        # query() 返回 dict: {"answer": ..., "session_id": ...}
        answer = result.get("answer", "")
        result_session_id = result.get("session_id", session_id)

        # 增加消息计数（用户提问 + AI 回答 = 2 条）
        await conversation_session_service.increment_message_count(
            session_id=session_id,
            count=2,
        )

        logger.info(f"[会话 {session_id}] 快速对话完成")

        return {
            "code": 200,
            "message": "success",
            "data": {
                "success": True,
                "answer": answer,
                "session_id": result_session_id,
                "errorMessage": None,
            },
        }

    except Exception as e:
        logger.error(f"对话接口错误: {e}")
        return {
            "code": 500,
            "message": "error",
            "data": {"success": False, "answer": None, "session_id": None, "errorMessage": str(e)},
        }


@router.post("/stream")
async def chat_stream(request: ChatRequest, current_user: User = Depends(get_current_user)):
    """流式对话接口（基于 RAG Agent，SSE）

    返回 SSE 格式，data 字段为 JSON：

    会话创建事件（仅在创建新会话时发送）:
    event: message
    data: {"type":"session_created","data":{"session_id":"xxx"}}

    工具调用事件:
    event: message
    data: {"type":"tool_call","data":{"tool":"工具名","status":"start|end","input":{...}}}

    内容流式事件:
    event: message
    data: {"type":"content","data":"内容块"}

    完成事件:
    event: message
    data: {"type":"done","data":{"answer":"完整答案","tool_calls":[...]}}

    Args:
        request: 对话请求

    Returns:
        SSE 事件流
    """
    # 如果 session_id 为空，创建新会话
    session_created = False
    if not request.session_id:
        session = await conversation_session_service.create_session(
            user_id=current_user.id,
            title=request.question[:50] if len(request.question) > 50 else request.question,
        )
        session_id = session.session_id
        session_created = True
        logger.info(f"创建新会话: {session_id}")
    else:
        session_id = request.session_id
        # 验证会话归属
        existing_session = await conversation_session_service.get_session(
            session_id=session_id,
            user_id=current_user.id,
        )
        if not existing_session:

            async def error_generator():
                yield {
                    "event": "message",
                    "data": json.dumps(
                        {"type": "error", "data": "会话不存在或无权访问"}, ensure_ascii=False
                    ),
                }

            return EventSourceResponse(error_generator())

    logger.info(f"[会话 {session_id}] 收到流式对话请求: {request.question}")

    async def event_generator():
        try:
            # 如果是新创建的会话，先发送 session_created 事件
            if session_created:
                yield {
                    "event": "message",
                    "data": json.dumps(
                        {"type": "session_created", "data": {"session_id": session_id}},
                        ensure_ascii=False,
                    ),
                }

            async for chunk in rag_agent_service.query_stream(
                request.question,
                session_id=session_id,
                user_id=str(current_user.id),
            ):
                chunk_type = chunk.get("type", "unknown")
                chunk_data = chunk.get("data", None)

                # 处理调试类型消息（新增）
                if chunk_type == "debug":
                    # 调试信息，可以选择发送或忽略
                    yield {
                        "event": "message",
                        "data": json.dumps(
                            {
                                "type": "debug",
                                "node": chunk.get("node", "unknown"),
                                "message_type": chunk.get("message_type", "unknown"),
                            },
                            ensure_ascii=False,
                        ),
                    }
                elif chunk_type == "tool_call":
                    # 发送工具调用事件（可选，前端可以显示工具调用状态）
                    yield {
                        "event": "message",
                        "data": json.dumps(
                            {"type": "tool_call", "data": chunk_data}, ensure_ascii=False
                        ),
                    }
                elif chunk_type == "search_results":
                    # 发送检索结果（可选，前端可以忽略）
                    yield {
                        "event": "message",
                        "data": json.dumps(
                            {"type": "search_results", "data": chunk_data}, ensure_ascii=False
                        ),
                    }
                elif chunk_type == "content":
                    # 发送内容块 - 关键：data 必须是 JSON 字符串
                    yield {
                        "event": "message",
                        "data": json.dumps(
                            {"type": "content", "data": chunk_data}, ensure_ascii=False
                        ),
                    }
                elif chunk_type == "complete":
                    # 发送完成信号
                    yield {
                        "event": "message",
                        "data": json.dumps(
                            {"type": "done", "data": chunk_data}, ensure_ascii=False
                        ),
                    }
                elif chunk_type == "error":
                    # 发送错误信息
                    yield {
                        "event": "message",
                        "data": json.dumps(
                            {"type": "error", "data": str(chunk_data)}, ensure_ascii=False
                        ),
                    }

            logger.info(f"[会话 {session_id}] 流式对话完成")

            # 增加消息计数（用户提问 + AI 回答 = 2 条）
            await conversation_session_service.increment_message_count(
                session_id=session_id,
                count=2,
            )

        except Exception as e:
            logger.error(f"流式对话接口错误: {e}")
            yield {
                "event": "message",
                "data": json.dumps({"type": "error", "data": str(e)}, ensure_ascii=False),
            }

    return EventSourceResponse(event_generator())


@router.post("/clear", response_model=ApiResponse)
async def clear_session(request: ClearRequest, current_user: User = Depends(get_current_user)):
    """清空会话历史

    Args:
        request: 清空请求

    Returns:
        操作结果
    """
    try:
        success = await rag_agent_service.clear_chat_history(request.session_id)
        logger.info(f"清空会话: {request.session_id}, 结果: {success}")

        return ApiResponse(
            status="success" if success else "error",
            message="会话已清空" if success else "清空会话失败",
            data=None,
        )

    except Exception as e:
        logger.error(f"清空会话错误: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/session/{session_id}", response_model=SessionInfoResponse)
async def get_session_info(
    session_id: str, current_user: User = Depends(get_current_user)
) -> SessionInfoResponse:
    """查询会话历史

    Args:
        session_id: 会话 ID

    Returns:
        会话信息
    """
    try:
        history = await rag_agent_service.get_chat_history(session_id)

        return SessionInfoResponse(
            session_id=session_id, message_count=len(history), history=history
        )

    except Exception as e:
        logger.error(f"获取会话信息错误: {e}")
        raise HTTPException(status_code=500, detail=str(e))
