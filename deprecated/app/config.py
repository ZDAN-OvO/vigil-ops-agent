"""配置管理模块

使用 Pydantic Settings 实现类型安全的配置管理
"""

from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录（config.py 在 app/ 下，所以向上取一级）
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """应用配置"""

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 应用配置
    app_name: str = "VigilOpsAgent"
    app_version: str = "1.0.0"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 9999

    # DashScope 配置
    dashscope_api_key: str = "sk:dashscope_api_key"  # 默认空字符串，实际使用需从环境变量加载
    dashscope_model: str = "qwen-max"  # 对话模型
    dashscope_embedding_model: str = "text-embedding-v4"  # v4 支持多种维度（默认 1024）
    dashscope_biz_space_api_base: str = (
        "https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1"  # 业务空间的 api_base_url
    )
    dashscope_rerank_model: str = "qwen3-rerank"  # rerank 重排模型

    # Milvus 配置
    milvus_host: str = "localhost"
    milvus_port: int = 19530
    milvus_timeout: int = 10000  # 毫秒

    # RAG 配置
    rag_model: str = "qwen-max"  # 使用快速响应模型，不带扩展思考

    # 文档分块配置
    chunk_max_size: int = 800
    chunk_overlap: int = 100

    # MCP 服务配置
    mcp_cls_transport: str = "streamable-http"
    mcp_cls_url: str = "http://localhost:8003/mcp"
    mcp_monitor_transport: str = "streamable-http"
    mcp_monitor_url: str = "http://localhost:8004/mcp"

    # 定时 AIOps 配置
    enable_scheduled_aiops: bool = False
    scheduled_aiops_interval_seconds: int = 300
    scheduled_aiops_webhook_url: str = ""
    scheduled_aiops_session_id: str = "_scheduled_aiops"

    # Redis 配置（用于短期记忆 - 会话 checkpoint）
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str = ""  # 空字符串表示无密码
    redis_checkpoint_ttl: int = 1800  # checkpoint 过期时间，默认 30 分钟

    # 对话历史持久化配置（MySQL 备份 + Redis fallback）
    conversation_history_enabled: bool = True  # 是否启用对话历史持久化

    # MySQL 配置（仅用于用户鉴权 + 会话管理）
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_db: str = "vigil_ops_agent"
    mysql_user: str = "root"
    mysql_password: str = ""

    # PostgreSQL 配置（用于冷 checkpoint fallback + Store + 对话历史持久化）
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "vigil_ops_agent"
    postgres_user: str = "postgres"
    postgres_password: str = "postgres"

    @property
    def postgres_dsn(self) -> str:
        """PostgreSQL 同步 DSN（供 langgraph-checkpoint-postgres / langgraph-store-postgres 的 from_conn_string 使用）"""
        return f"postgresql://{self.postgres_user}:{self.postgres_password}@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"

    @property
    def postgres_async_url(self) -> str:
        """PostgreSQL 异步连接 URL（供 SQLAlchemy asyncpg 驱动使用）"""
        return f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"

    # Store 配置（LangGraph BaseStore 实现，底层已迁移至 PostgreSQL）
    store_table_name: str = "store_items"  # Store 表名
    store_enabled: bool = True  # 是否启用 Store（用户画像长期记忆）

    # 长期记忆配置（用户画像特征提取）
    long_term_memory_enabled: bool = True  # 是否启用长期记忆中间件
    memory_extraction_model: str = ""  # 特征提取使用的模型，空字符串时复用主模型

    # JWT 认证配置
    jwt_secret_key: str = "change-me-in-production-use-a-random-string"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7
    access_token_cache_ttl_seconds: int = 259200  # Access Token Redis 缓存 TTL，默认 3 天（秒）

    # Cookie 配置（HttpOnly + Secure + SameSite=Strict）
    access_token_cookie_name: str = "access_token"
    refresh_token_cookie_name: str = "refresh_token"
    cookie_secure: bool = False  # 生产环境 HTTPS 时设为 True
    cookie_samesite: str = "strict"  # strict 防 CSRF
    cookie_httponly: bool = True  # JS 无法读取
    cookie_max_age_access: int = 1800  # 30 分钟（秒）接口访问 token 过期时间
    cookie_max_age_refresh: int = 604800  # 7 天（秒）刷新“接口访问 token”的 token 过期时间

    # 初始管理员配置（首次启动时自动创建）
    initial_admin_username: str = "admin"
    initial_admin_password: str = "admin123"

    @property
    def mcp_servers(self) -> dict[str, dict[str, Any]]:
        """获取完整的 MCP 服务器配置"""
        return {
            "cls": {
                "transport": self.mcp_cls_transport,
                "url": self.mcp_cls_url,
            },
            "monitor": {
                "transport": self.mcp_monitor_transport,
                "url": self.mcp_monitor_url,
            },
        }


# 全局配置实例
config = Settings()

if __name__ == "__main__":
    print(Settings())
