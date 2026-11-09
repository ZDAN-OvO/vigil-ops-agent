# Vigil AIOps

面向 Kubernetes 故障调查与受控修复的轻量项目。2026-11-08 完成完整 MVP（MS01–MS08）：三个核心场景、两类真实动作、审批恢复、幂等与评估门槛 V01–V10；已知缺口见 [demos/ms08/README.md](demos/ms08/README.md)。

项目直接位于仓库根目录，源码在 [src/aiops/](src/aiops/)，旧项目原样归档至 [deprecated/](deprecated/README.md)。

**先看 [操作手册](docs/usage.md)**：环境前置、`.env` 配置、命令与退出码、一条可复现的坏发布演练。

```sh
make init
make check-all
make help
uv run --frozen aiops status --output json
uv run --frozen aiops db status          # 需要已配置 VIGIL_DATABASE_URL
uv run --frozen aiops incidents
uv run --frozen aiops investigate --service frontend --symptoms "首页 500" --mode rules
```

`make init` 使用 uv 安装锁定依赖及开发工具，也可直接运行 `uv sync --group dev`。`status` 报告实现范围，不探测集群，也不声称依赖健康。`investigate`、`act`、`approve`、`resume`、`explain`、`eval`、`serve`、`knowledge` 等命令已实现，用法见 `aiops --help` 与各 `demos/`。

配置变量使用 `VIGIL_` 前缀；可将 [.env.example](.env.example) 复制为根目录 `.env`。配置文件与相对运行路径始终以仓库根目录为基准，不随启动目录改变。业务记录与审计依赖 PostgreSQL；Agent 调查与 Explain 另需模型网关，规则基线评估与直接执行不需要。配置项见 `.env.example`。

- [文档导航](docs/README.md)
- [已有决策与待确认事项](docs/decisions.md)
- [领域词汇表](GLOSSARY.md)
- [总体架构](docs/architecture.md)
- [模块契约](docs/contracts.md)
- [MVP 范围与验收](docs/mvp.md)
- [里程碑与验收](docs/milestone.md)
- [滚动迭代计划](docs/iterations.md)
- [评估约定](docs/eval.md)
- [归档校验与迁移说明](docs/migration.md)

LangGraph、PostgreSQL checkpoint、模型客户端与 Kubernetes、HTTP 依赖已锁定；pgvector 与向量检索按后续里程碑接入。旧锁文件和旧 `.env` 保留在归档目录，新项目不读取归档配置。旧 `.env` 在迁移前已被 Git 跟踪，本次没有清理 Git 历史。
