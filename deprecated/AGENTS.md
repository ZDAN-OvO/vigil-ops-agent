# AGENTS.md

## 语言规范

- 对话、注释、文档使用**简体中文**；代码标识符使用**英文**。
- Commit Message 使用简体中文。

## 技术栈

| 项 | 选择 |
|----|------|
| Python | `>=3.11,<3.14`（当前 3.13） |
| 包管理 | **uv**（非 pip/poetry） |
| Web 框架 | FastAPI + uvicorn |
| Agent 框架 | LangGraph + langchain-qwq（ChatQwen 原生集成） |
| 向量库 | Milvus（`langchain-milvus` + `pymilvus`） |
| MCP | `langchain-mcp-adapters` + `fastmcp` |
| 日志 | **Loguru**（`from loguru import logger`，非标准 logging） |
| 数据库 | MySQL（`aiomysql` + `SQLAlchemy`，仅鉴权+会话）+ PostgreSQL（`psycopg3` + `asyncpg`，冷 checkpoint + Store + 对话历史）+ Redis（热 checkpoint + 消息队列） |

## 常用命令

```bash
# 环境初始化
uv pip install -e .            # 安装依赖
uv pip install -e ".[dev]"     # 安装开发依赖
make init                      # 一键初始化（Docker Milvus + 服务 + 上传文档）

# 服务管理
make start                     # 启动全部（MCP + FastAPI）
make stop                      # 停止全部
make restart                   # 重启全部
make dev                       # 开发模式（前台热重载）
make status-mcp                # 查看 MCP 服务状态
make logs                      # 查看服务日志
.\start-windows.bat            # Windows 启动
.\stop-windows.bat             # Windows 停止

# 代码质量（PR 前必须通过）
make format                    # ruff isort 修复 + ruff format
make lint                      # ruff check
make fix                       # ruff check --fix + format 自动修复
make test                      # pytest --cov
make test-quick                # 快速测试（无覆盖率）
make check-all                 # format + lint + test
make coverage                  # 查看覆盖率报告
make type-check                # mypy 类型检查
make security                  # bandit 安全检查

# 文档向量化
make upload                    # 上传 aiops-docs/*.md 到 Milvus

# 依赖管理
make add PKG=xxx               # 添加生产依赖
make add-dev PKG=xxx           # 添加开发依赖
make remove PKG=xxx            # 移除依赖
make clean                     # 清理临时文件

# 开发辅助
make shell                     # 启动 Python shell（config 可用）
make ipython                   # 启动 IPython shell
make docs                      # 打开 API 文档（浏览器）
make watch                     # 监视文件变化自动运行测试
make pre-commit-install        # 安装 pre-commit hooks
make pre-commit                # 运行 pre-commit 检查
```

## 服务与端口

| 服务 | 端口 | 入口 |
|------|------|------|
| FastAPI 主服务 | `9999` | `uvicorn app.main:app` |
| CLS MCP 服务 | `8003` | `mcp_servers/cls_server.py` |
| Monitor MCP 服务 | `8004` | `mcp_servers/monitor_server.py` |
| Milvus（含 Attu `:8000`、MinIO `:9001`） | `19530` | Docker `milvus-standalone` v2.5.10 |
| Redis | `6379` | Docker `redis:8.0` |
| PostgreSQL | `5432` | Docker `postgres:17` |
| MySQL | `3306` | 外部实例（非 Docker） |

## 架构要点

分层：`api/`（路由薄层）→ `services/`（业务逻辑）→ `agent/`（LangGraph 节点 + MCP 客户端）→ `tools/`（工具）→ `models/`（`dto/` 请求响应 + `entity/` ORM 实体）→ `core/`（基础设施：LLM 工厂、DB 连接管理 `manager/`、认证、特征提取）→ `utils/`。

### 目录结构补充

```
app/
├── services/
│   ├── scheduler/aiops_scheduler.py    # 定时 AIOps 诊断（Webhook 回调）
│   └── vector/                         # 向量服务（store/embedding/index/search/rerank）
├── core/
│   ├── manager/                        # DB 连接管理（milvus/mysql/postgres/redis）
│   ├── auth_resolver.py                # JWT 认证核心（依赖注入）
│   ├── document_splitter.py            # 文档分割（LangChain）
│   ├── feature_extractor.py            # LLM 提取结构化画像
│   ├── feature_extraction_middleware.py# Agent 后处理中间件（自动提取画像）
│   └── profile_reducer.py              # 画像归约（Reducer 模式）
├── agent/
│   ├── aiops/                          # Plan-Execute-Replan（state/planner/executor/replanner）
│   ├── context.py                      # AgentContext 运行时上下文（dataclass）
│   └── mcp_client.py                   # MultiServerMCPClient 聚合 + 重试
├── models/
│   ├── document.py                     # DocumentChunk 文档分块模型
│   ├── dto/                            # 请求/响应 Pydantic 模型
│   └── entity/                         # SQLAlchemy ORM 实体
├── tools/                              # Agent 工具（knowledge_tool / time_tool）
└── utils/
    ├── logger.py                       # Loguru 日志配置
    └── redis_queue.py                  # Redis 消息队列（三级降级架构）
```

### 关键模式

- **全局服务单例**：`rag_agent_service`、`vector_store_manager` 等模块级实例，直接 `import` 使用。
- **MCP 聚合**：`agent/mcp_client.py` 用 `MultiServerMCPClient` 连接多个 MCP 服务器，`retry_interceptor` 提供指数退避重试。
- **SSE 流式**：`/api/chat/stream` 和 `/api/aiops` 通过 `sse-starlette` 返回 JSON 事件。
- **三级消息降级**：`utils/redis_queue.py` — Redis List → `asyncio.Queue` 内存 → 兜底日志文件（`logs/mq_fallback.jsonl`）。
- **双层记忆架构**：Redis（热 checkpoint，TTL=7天）+ PostgreSQL（冷 checkpoint fallback + Store + 对话历史持久化）。Redis miss 时自动从 PostgreSQL 恢复。
- **LangGraph 官方实现**：Checkpointer 使用 `AsyncPostgresSaver`（`langgraph-checkpoint-postgres`），Store 使用 `AsyncPostgresStore`（`langgraph-store-postgres`），均通过 `from_conn_string()` 内置连接管理。
- **长期记忆（用户画像）**：`FeatureExtractionMiddleware` 在 Agent 执行后自动从对话中提取画像 → `FeatureExtractor`（LLM 提取）→ `ProfileReducer`（归约合并）→ `LongTermMemoryService`（Store CRUD）→ 注入 system prompt。
- **AgentContext 运行时上下文**：`agent/context.py` 定义 `AgentContext` dataclass，通过 `runtime.context` 在 Middleware 中访问 `user_id` 等信息。
- **认证三层联动**：`core/auth_resolver.py`（JWT 核心 + 依赖注入）→ `services/auth_service.py`（业务逻辑）→ `api/auth.py`（路由），修改时需三层联动。

### API 路由

`/health` 健康检查 · `/api/auth` 认证 · `/api/chat` 对话（SSE） · `/api/sessions` 会话管理 · `/api/file` 文件上传与索引 · `/api/aiops` AIOps 智能运维（SSE）

静态文件 `static/` 挂载在 `/static`，`/` 返回 `index.html`。

### 两条核心路径

- **RAG 对话**：`chat.py` → `rag_agent_service` → LangGraph Agent（含 MCP 工具调用 + Milvus 检索 + 画像注入）→ SSE 流式 → MQ 异步持久化 PostgreSQL
- **AIOps 诊断**：`aiops.py` → `aiops_service` → `agent/aiops/`（Plan-Execute-Replan 循环）→ SSE 流式

### 数据库职责划分

- **MySQL**：用户鉴权（`auth_service`）+ 会话管理（`conversation_session_service`）
- **PostgreSQL**：冷 checkpoint fallback（`AsyncPostgresSaver`）+ 用户画像 Store（`AsyncPostgresStore`）+ 对话历史持久化
- **Redis**：热 checkpoint（`AsyncRedisSaver`）+ 消息队列（`redis_queue.py`）+ Token 缓存

## 禁止事项

- **不要**直接修改 `core/redis_checkpointer.py`、`core/manager/` 下的四个客户端文件，除非明确要求。
- **不要**修改 MCP 服务器接口（`mcp_servers/`）而不通知。
- **不要**使用 `pip install`，始终使用 `uv pip install`。
- **不要**使用标准 `logging`，始终使用 `from loguru import logger`。
- **不要**硬编码环境变量，所有配置通过 `config.py`（Pydantic Settings）从 `.env` 加载。
- **不要**在 `app/` 下创建新的顶层包，除非讨论过。
- **不要**混用 `ChatOpenAI` 和 `langchain-qwq`，后者是 ChatQwen 原生集成。

## 易踩坑

- `DASHSCOPE_API_BASE` 必须为 `https://dashscope.aliyuncs.com/compatible-mode/v1`，否则默认访问新加坡站点导致鉴权失败。
- `langchain-qwq` 是 ChatQwen 原生集成，非通用 OpenAI 兼容适配器，不要混用 `ChatOpenAI`。
- 虚拟环境在 `.venv/`，由 uv 管理，不要手动创建或激活其他虚拟环境。
- `RERANK_ENABLED` 默认 `False`，启用 Rerank 需要同时在 `.env` 中配置 `DASHSCOPE_RERANK_MODEL`。
- `AsyncPostgresSaver.from_conn_string()` 和 `AsyncPostgresStore.from_conn_string()` 内置连接管理，不要自建 manager 包装。
- Docker Compose 文件名为 `docker_compose.yml`（非 `vector-database.yml`）。
- `AgentContext` 通过 `config={"configurable": {"thread_id": session_id}}` + `context=AgentContext(user_id=...)` 传入，Middleware 中通过 `runtime.context` 访问。
- `FeatureExtractionMiddleware` 依赖 `LongTermMemoryService` 和 `FeatureExtractor`，注册到 LangGraph Agent 的 `middleware` 列表中。

## 模块约束

- 单模块目标 **< 500 行**（不含测试）。超过约 800 行应拆分。
- `services/` 中新增服务时，保持单一职责，一个文件对应一个领域。
- 新增 Agent 工具放在 `tools/`，遵循 `knowledge_tool.py` 的签名模式。
- 向量相关服务放在 `services/vector/` 子目录，按职责拆分（store/embedding/index/search/rerank）。

## 完成标准

修改代码后，满足以下条件才算完成：

1. `make format && make lint` 无错误
2. `make test` 全部通过（新增功能需补充测试）
3. 新增 API 端点必须在 `models/dto/` 中有对应的请求/响应模型
4. 新增环境变量必须在 `config.py` 中声明并设置合理默认值
5. 日志使用 `logger`（Loguru），不使用 `print`

## 验证命令

| 变更类型 | 验证命令 |
|----------|----------|
| 业务逻辑 / API | `make check-all` |
| 向量检索 / RAG | `make test` + `curl` 测试 `/api/chat/stream` |
| MCP 工具 | `make status-mcp` |
| 配置变更 | `make start` + `curl localhost:9999/health` |
| 认证 / 会话 | `make test` + 测试 `/api/sessions` CRUD |
| 长期记忆 | 检查 Store `("user_profiles", user_id)` namespace |
