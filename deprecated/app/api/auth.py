"""认证接口

提供用户登录、登出、令牌刷新、用户管理等接口。
"""

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from loguru import logger

from app.config import config
from app.core.auth_resolver import (
    clear_auth_cookies,
    create_access_token,
    create_refresh_token,
    get_current_user,
    is_access_token_valid,
    set_auth_cookies,
    verify_access_token,
)
from app.models.dto.auth_request import (
    LoginRequest,
    LoginResponse,
    UserCreateRequest,
    UserInfoResponse,
    UserListResponse,
    UserUpdateRequest,
)
from app.models.entity.user import User
from app.services.auth_service import auth_service

router = APIRouter()


@router.post("/login", response_model=LoginResponse)
async def login(request: LoginRequest, response: Response):
    """用户登录

    使用用户名和密码登录，通过 HttpOnly Cookie 返回 access_token 和 refresh_token。

    Args:
        request: 登录请求
        response: FastAPI 响应对象（用于设置 Cookie）

    Returns:
        LoginResponse: 包含用户信息（token 通过 Cookie 传递）

    Raises:
        HTTPException: 登录失败时抛出 401
    """
    user = await auth_service.authenticate(request.username, request.password)

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 清理该用户的所有旧 token（单设备模式：新登录会使旧会话失效）
    await auth_service.revoke_all_user_tokens(user.id)

    # 签发令牌
    access_token = create_access_token(user.id, user.username)
    refresh_token = create_refresh_token()

    # 存储到 Redis
    await auth_service.store_refresh_token(refresh_token, user.id)
    await auth_service.store_access_token(access_token, user.id)

    # 设置 HttpOnly Cookie
    set_auth_cookies(response, access_token, refresh_token)

    logger.info(f"用户登录成功: {user.username}")

    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        user=UserInfoResponse.model_validate(user),
    )


@router.post("/refresh")
async def refresh_token(
    response: Response,
    access_token: str | None = Cookie(default=None, alias=config.access_token_cookie_name),
    refresh_token: str | None = Cookie(default=None, alias=config.refresh_token_cookie_name),
):
    """刷新 Access Token

    仅当当前 access_token 已过期或无效时才签发新 token，避免无谓刷新。
    从 HttpOnly Cookie 中读取 refresh_token 和 access_token：
    - access_token 仍有效 → 直接返回成功，不签发新 token
    - access_token 已过期/无效 → 用 refresh_token 签发新 access_token
    - refresh_token 无效/过期 → 返回 401，前端跳转登录页

    Args:
        response: FastAPI 响应对象
        access_token: 从 Cookie 中读取的 access_token
        refresh_token: 从 Cookie 中读取的 refresh_token

    Returns:
        dict: 刷新结果（包含 refreshed 字段标识是否实际签发了新 token）

    Raises:
        HTTPException: 刷新失败时抛出 401
    """
    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的刷新令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = await auth_service.verify_refresh_token(refresh_token)

    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="刷新令牌已过期，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 获取用户信息
    user = await auth_service.get_user_by_id(user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在或已禁用",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 检查当前 access_token 是否仍然有效（JWT 未过期 + Redis 缓存存在）
    # 若有效则直接复用，无需签发新 token
    if access_token and is_access_token_valid(access_token):
        token_in_redis = await auth_service.verify_access_token_cached(access_token)
        if token_in_redis:
            logger.debug(f"access_token 仍有效，跳过刷新: user_id={user_id}")
            return {"message": "token 仍有效，无需刷新", "refreshed": False}

    # access_token 已过期或无效，签发新 token
    # 清理该用户所有旧的 access_token，再存入新的（防止 token 堆积）
    await auth_service.revoke_all_user_access_tokens(user_id)

    # 签发新的 access_token
    new_access_token = create_access_token(user.id, user.username)
    # 存储新 access_token 到 Redis
    await auth_service.store_access_token(new_access_token, user.id)

    # 更新 access_token Cookie（refresh_token 不变，无需重新设置）
    response.set_cookie(
        key=config.access_token_cookie_name,
        value=new_access_token,
        max_age=config.cookie_max_age_access,
        httponly=config.cookie_httponly,
        secure=config.cookie_secure,
        samesite=config.cookie_samesite,
        path="/",
    )

    logger.debug(f"已刷新 access_token: user_id={user_id}")
    return {"message": "token 已刷新", "refreshed": True}


@router.post("/logout")
async def logout(
    response: Response,
    access_token: str | None = Cookie(default=None, alias=config.access_token_cookie_name),
):
    """用户登出

    清空该用户在 Redis 中的所有 token 信息（Access Token + Refresh Token），立即失效，
    并清除浏览器中的认证 Cookie。

    Args:
        response: FastAPI 响应对象
        access_token: 从 Cookie 中读取的 access_token

    Returns:
        dict: 登出结果
    """
    if not access_token:
        logger.warning("[logout] Cookie 中无 access_token")
        # 即使没有 token，也清除 Cookie（确保浏览器干净）
        clear_auth_cookies(response)
        return {"message": "登出成功"}

    logger.debug(f"[logout] 收到登出请求，access_token 前16位: {access_token[:16]}...")

    # 从 access_token 中解析 user_id，用于批量清理该用户的所有 token
    payload = verify_access_token(access_token)
    if payload is None:
        logger.warning("[logout] access_token 解析失败，仅清除 Cookie")
        clear_auth_cookies(response)
        return {"message": "登出成功"}

    user_id = int(payload["sub"])
    logger.debug(f"[logout] 从 JWT 解析出 user_id={user_id}, username={payload.get('username')}")

    # 批量清空该用户在 Redis 中的所有 token（Access Token + Refresh Token）
    await auth_service.revoke_all_user_tokens(user_id)

    # 清除浏览器中的认证 Cookie
    clear_auth_cookies(response)

    logger.info(f"用户登出成功，已清空所有 token (user_id={user_id})")
    return {"message": "登出成功"}


@router.get("/me", response_model=UserInfoResponse)
async def get_current_user_info(current_user: User = Depends(get_current_user)):
    """获取当前登录用户信息

    Args:
        current_user: 当前用户（通过 JWT 自动注入）

    Returns:
        UserInfoResponse: 用户信息
    """
    return UserInfoResponse.model_validate(current_user)


# ========== 管理员接口 ==========


@router.post("/users", response_model=UserInfoResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    request: UserCreateRequest,
    current_user: User = Depends(get_current_user),
):
    """创建新用户（仅管理员）

    Args:
        request: 创建用户请求
        current_user: 当前用户（需为管理员）

    Returns:
        UserInfoResponse: 创建的用户信息

    Raises:
        HTTPException: 权限不足或用户名已存在
    """
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅管理员可创建用户",
        )

    try:
        user = await auth_service.create_user(
            username=request.username,
            password=request.password,
            display_name=request.display_name,
            is_admin=request.is_admin,
        )
        return UserInfoResponse.model_validate(user)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.get("/users", response_model=UserListResponse)
async def list_users(
    offset: int = 0,
    limit: int = 50,
    current_user: User = Depends(get_current_user),
):
    """获取用户列表（仅管理员）

    Args:
        offset: 偏移量
        limit: 每页数量
        current_user: 当前用户（需为管理员）

    Returns:
        UserListResponse: 用户列表

    Raises:
        HTTPException: 权限不足
    """
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅管理员可查看用户列表",
        )

    users, total = await auth_service.list_users(offset=offset, limit=limit)

    return UserListResponse(
        users=[UserInfoResponse.model_validate(u) for u in users],
        total=total,
    )


@router.put("/users/{user_id}", response_model=UserInfoResponse)
async def update_user(
    user_id: int,
    request: UserUpdateRequest,
    current_user: User = Depends(get_current_user),
):
    """更新用户信息（仅管理员）

    Args:
        user_id: 用户 ID
        request: 更新请求
        current_user: 当前用户（需为管理员）

    Returns:
        UserInfoResponse: 更新后的用户信息

    Raises:
        HTTPException: 权限不足或用户不存在
    """
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅管理员可更新用户",
        )

    user = await auth_service.update_user(
        user_id=user_id,
        display_name=request.display_name,
        is_active=request.is_active,
        is_admin=request.is_admin,
        password=request.password,
    )

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在",
        )

    return UserInfoResponse.model_validate(user)


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    current_user: User = Depends(get_current_user),
):
    """删除用户（仅管理员）

    Args:
        user_id: 用户 ID
        current_user: 当前用户（需为管理员）

    Returns:
        dict: 删除结果

    Raises:
        HTTPException: 权限不足或用户不存在
    """
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅管理员可删除用户",
        )

    # 禁止删除自己
    if user_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不能删除自己",
        )

    success = await auth_service.delete_user(user_id)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在",
        )

    return {"message": "用户已删除"}
