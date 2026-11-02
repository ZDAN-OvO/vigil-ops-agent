# Vigil AIOps

面向 Kubernetes 故障调查与受控修复的轻量项目。当前是 **W0：目录隔离、CLI 骨架、架构与接口契约**；集群、调查、RAG、写操作、自动回滚尚未实现。

项目直接位于仓库根目录，源码在 [src/aiops/](src/aiops/)，旧项目原样归档至 [deprecated/](deprecated/README.md)。

```sh
make init
make check-all
make help
uv run --frozen aiops status --output json
```

`make init` 使用 uv 安装锁定依赖及开发工具，也可直接运行 `uv sync --group dev`。`status` 报告实现范围，不探测集群，也不声称依赖健康。`investigate`、`act`、`explain`、`eval` 等业务命令将在各阶段加入，现在没有可执行占位命令。

配置变量使用 `VIGIL_` 前缀；可将 [.env.example](.env.example) 复制为根目录 `.env`。配置文件与相对运行路径始终以仓库根目录为基准，不随启动目录改变。W0 不需要模型密钥或数据库。

- [架构与决策记录](docs/architecture.md)
- [模块契约](docs/contracts.md)
- [阶段产出与验收](docs/roadmap.md)
- [评估约定](docs/eval.md)
- [归档校验与迁移说明](docs/migration.md)

按实际接入阶段再锁定 LangGraph、PostgreSQL/pgvector、模型集成和信号客户端依赖；当前锁文件只包含骨架真正使用的依赖。旧锁文件和旧 `.env` 保留在归档目录，新项目不读取归档配置。旧 `.env` 在迁移前已被 Git 跟踪，本次没有清理 Git 历史。
