"""认证核心模块

提供 JWT 令牌签发/验证、密码哈希、FastAPI 依赖注入等认证基础设施。
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from fastapi import Cookie, HTTPException, Request, Response, status
from fastapi.security import HTTPBearer
from loguru import logger

from app.config import config

# Bearer Token 提取（保留用于兼容）
security = HTTPBearer()


def hash_password(password: str) -> str:
    """
    对明文密码进行 bcrypt 哈希

    Args:
        password: 明文密码

    Returns:
        str: 哈希后的密码字符串
    """
    password_bytes = password.encode("utf-8")
    salt = bcrypt.gensalt(rounds=12)
    hashed = bcrypt.hashpw(password_bytes, salt)
    return hashed.decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    验证明文密码是否与哈希值匹配

    Args:
        plain_password: 明文密码
        hashed_password: 哈希后的密码

    Returns:
        bool: 匹配返回 True
    """
    password_bytes = plain_password.encode("utf-8")
    hashed_bytes = hashed_password.encode("utf-8")
    return bcrypt.checkpw(password_bytes, hashed_bytes)


def create_access_token(user_id: int, username: str) -> str:
    """
    签发 Access Token（JWT）

    Args:
        user_id: 用户 ID
        username: 用户名

    Returns:
        str: JWT 字符串
    """
    expire = datetime.now(timezone.utc) + timedelta(minutes=config.access_token_expire_minutes)
    payload = {
        "sub": str(user_id),
        "username": username,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "type": "access",
    }
    return jwt.encode(payload, config.jwt_secret_key, algorithm=config.jwt_algorithm)


def create_refresh_token() -> str:
    """
    生成 Refresh Token（UUID）

    Returns:
        str: UUID 字符串作为 refresh token
    """
    import uuid

    return str(uuid.uuid4())


def verify_access_token(token: str) -> Optional[dict]:
    """
    验证并解析 Access Token

    Args:
        token: JWT 字符串

    Returns:
        dict: 解码后的 payload，验证失败返回 None
    """
    try:
        payload = jwt.decode(token, config.jwt_secret_key, algorithms=[config.jwt_algorithm])

        # 验证 token 类型
        if payload.get("type") != "access":
            logger.warning(f"Token 类型不匹配: {payload.get('type')}")
            return None

        # 验证必要字段
        if "sub" not in payload or "username" not in payload:
            logger.warning("Token 缺少必要字段")
            return None

        return payload

    except jwt.ExpiredSignatureError:
        logger.debug("Access Token 已过期")
        return None
    except jwt.InvalidTokenError as e:
        logger.warning(f"无效的 Access Token: {e}")
        return None


def is_access_token_valid(token: str) -> bool:
    """
    同步检查 Access Token 是否有效（JWT 签名正确 + 未过期 + 类型/字段完整）

    不检查 Redis 缓存（缓存检查需异步调用 verify_access_token_cached）。
    用于 refresh 端点判断当前 access_token 是否仍可复用，避免无谓签发新 token。

    Args:
        token: JWT 字符串

    Returns:
        bool: 有效返回 True，无效/过期/格式错误返回 False
    """
    return verify_access_token(token) is not None


async def get_current_user(
    request: Request,
    access_token: str | None = Cookie(default=None, alias=config.access_token_cookie_name),
) -> "User":
    """
    FastAPI 依赖注入：从 HttpOnly Cookie 中提取并验证 JWT，返回当前用户

    优先从 Cookie 读取 access_token（HttpOnly，JS 不可读），
    若 Cookie 中不存在则回退到 Authorization header（兼容 API 客户端）。

    Args:
        request: FastAPI 请求对象
        access_token: 从 Cookie 中读取的 access_token

    Returns:
        User: 当前登录的用户对象

    Raises:
        HTTPException: 认证失败时抛出 401
    """
    from app.services.auth_service import auth_service

    # 优先从 Cookie 读取，回退到 Authorization header
    token = access_token
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未提供认证令牌，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = verify_access_token(token)

    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的认证令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 检查 Access Token 是否在 Redis 缓存中存在（防止已吊销的 token 继续使用）
    token_valid = await auth_service.verify_access_token_cached(token)
    if not token_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="认证令牌已被吊销，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = int(payload["sub"])
    user = await auth_service.get_user_by_id(user_id)

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 校验 JWT 中的 username 与数据库是否一致（防止用户名修改后旧 token 仍有效）
    if payload.get("username") != user.username:
        logger.warning(
            f"Token username 与数据库不一致: token={payload.get('username')}, db={user.username}"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="令牌信息与数据库不一致，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="用户已被禁用",
        )

    return user


def set_auth_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    """
    在响应中设置 HttpOnly Cookie

    Args:
        response: FastAPI 响应对象
        access_token: JWT access token
        refresh_token: UUID refresh token
    """
    response.set_cookie(
        key=config.access_token_cookie_name,
        value=access_token,
        max_age=config.cookie_max_age_access,
        httponly=config.cookie_httponly,
        secure=config.cookie_secure,
        samesite=config.cookie_samesite,
        path="/",
    )
    response.set_cookie(
        key=config.refresh_token_cookie_name,
        value=refresh_token,
        max_age=config.cookie_max_age_refresh,
        httponly=config.cookie_httponly,
        secure=config.cookie_secure,
        samesite=config.cookie_samesite,
        path="/api/auth/refresh",  # 仅刷新接口可用，限制作用域
    )


def clear_auth_cookies(response: Response) -> None:
    """
    在响应中清除认证 Cookie

    Args:
        response: FastAPI 响应对象
    """
    response.delete_cookie(
        key=config.access_token_cookie_name,
        path="/",
    )
    response.delete_cookie(
        key=config.refresh_token_cookie_name,
        path="/api/auth/refresh",
    )


# 类型提示用（避免循环导入）
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.entity.user import User  # noqa: F401
