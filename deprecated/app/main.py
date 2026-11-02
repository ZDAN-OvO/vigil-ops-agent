"""FastAPI 应用入口

主应用程序，配置路由、中间件、静态文件等
"""

import os
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres import AsyncPostgresStore
from loguru import logger

from app.api import aiops, auth, chat, file, health, sessions
from app.config import config
from app.core.manager.milvus_client import milvus_manager
from app.core.manager.mysql_client import mysql_manager
from app.core.manager.postgres_client import postgres_manager
from app.core.manager.redis_client import redis_manager
from app.services.auth_service import auth_service
from app.services.conversation_session_service import conversation_session_service
from app.services.scheduler.aiops_scheduler import scheduled_aiops_service


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时执行
    logger.info("=" * 60)
    logger.info(f"🚀 {config.app_name} v{config.app_version} 启动中...")
    logger.info(f"📝 环境: {'开发' if config.debug else '生产'}")
    logger.info(f"🌐 监听地址: http://{config.host}:{config.port}")
    logger.info(f"📚 API 文档: http://{config.host}:{config.port}/docs")

    # 连接 Milvus
    logger.info("🔌 正在连接 Milvus...")
    milvus_manager.connect()
    logger.info("✅ Milvus 连接成功")

    # 连接 Redis（用于短期记忆 - 会话 checkpoint）
    logger.info("🔌 正在连接 Redis...")
    await redis_manager.connect()
    logger.info("✅ Redis 连接成功")

    # 连接 MySQL（用于用户认证 + 会话管理）
    logger.info("🔌 正在连接 MySQL...")
    await mysql_manager.connect()
    logger.info("✅ MySQL 连接成功")

    # 连接 PostgreSQL（用于冷 checkpoint fallback + Store + 对话历史持久化）
    logger.info("🔌 正在连接 PostgreSQL...")
    await postgres_manager.connect()
    logger.info("✅ PostgreSQL 连接成功")

    from app.services.rag_agent_service import rag_agent_service

    # 使用 AsyncExitStack 管理 LangGraph PostgreSQL 上下文管理器
    # from_conn_string() 返回 asynccontextmanager，必须在整个应用生命周期内保持打开
    async with AsyncExitStack() as stack:
        # 初始化 LangGraph 官方 Store（长期记忆 - 用户画像，基于 PostgreSQL）
        if config.store_enabled:
            logger.info("📊 正在初始化 LangGraph Store（PostgreSQL）...")
            pg_store = await stack.enter_async_context(
                AsyncPostgresStore.from_conn_string(config.postgres_dsn)
            )
            await pg_store.setup()
            rag_agent_service.set_store(pg_store)
            logger.info("✅ LangGraph Store 初始化完成")

        # 初始化冷 Checkpointer（PostgreSQL，Redis TTL 过期后 fallback）
        if config.conversation_history_enabled:
            logger.info("🧊 正在初始化 PostgreSQL 冷 Checkpointer...")
            apg_saver = await stack.enter_async_context(
                AsyncPostgresSaver.from_conn_string(config.postgres_dsn)
            )
            await apg_saver.setup()
            rag_agent_service.set_postgres_saver(apg_saver)
            logger.info("✅ PostgreSQL 冷 Checkpointer 初始化完成")

        # 初始化会话管理表
        logger.info("📝 正在初始化会话管理表...")
        await conversation_session_service.init_db()
        logger.info("✅ 会话管理表初始化完成")

        # 初始化用户认证表并创建初始管理员
        logger.info("🔐 正在初始化用户认证系统...")
        await auth_service.init_db()
        logger.info("✅ 用户认证系统初始化完成")

        # 启动 checkpoint 持久化消费者（BLPOP 模式）
        await rag_agent_service.start_persistence_consumer()

        # 启动定时 AIOps 任务
        if config.enable_scheduled_aiops:
            await scheduled_aiops_service.start()

        logger.info("=" * 60)

        yield

        # 关闭时执行
        await rag_agent_service.stop_persistence_consumer()
        await scheduled_aiops_service.stop()

    # AsyncExitStack 退出时自动关闭 pg_store / apg_saver 的数据库连接

    logger.info("🔌 正在关闭连接...")
    milvus_manager.close()
    await redis_manager.close()
    await postgres_manager.close()
    await mysql_manager.close()
    logger.info(f"👋 {config.app_name} 关闭")


# 创建 FastAPI 应用
app = FastAPI(
    title=config.app_name,
    version=config.app_version,
    description="基于 LangChain 的 Vigil 智能运维系统",
    lifespan=lifespan,
)

# 配置 CORS（Cookie 模式下必须指定具体域名，不能用 "*"）
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        f"http://localhost:{config.port}",
        f"http://127.0.0.1:{config.port}",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(health.router, tags=["健康检查"])
app.include_router(auth.router, prefix="/api/auth", tags=["认证"])
app.include_router(chat.router, prefix="/api/chat", tags=["对话"])
app.include_router(sessions.router, prefix="/api/sessions", tags=["会话管理"])
app.include_router(file.router, prefix="/api/file", tags=["文件管理"])
app.include_router(aiops.router, prefix="/api/aiops", tags=["AIOps智能运维"])

# 挂载静态文件
static_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def root():
    """返回首页"""
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {
        "message": f"Welcome to {config.app_name} API",
        "version": config.app_version,
        "docs": "/docs",
    }


@app.get("/login")
async def login_page():
    """返回登录页面"""
    login_path = os.path.join(static_dir, "login.html")
    if os.path.exists(login_path):
        return FileResponse(login_path)
    return {"message": "Login page not found"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app", host=config.host, port=config.port, reload=config.debug, log_level="info"
    )
