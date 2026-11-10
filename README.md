# Vigil AIOps

Kubernetes 故障调查与受控处置。真实告警进来，模型在有界预算内取证，规则或人工审批后执行限定动作，再用真实请求验证恢复，不看 Pod Ready。

一次真实链路（2026-11-10，本机 k3d 实验集群）：坏发布让首页 500，告警 firing，调查完成并给出 14 条证据，生成回退提案，人工审批后执行 `rollout_undo`，首页 200、告警清零、坏 ReplicaSet 副本 0。

完整 MVP（MS01–MS08）已交付：三个核心场景、两类真实动作、审批跨进程恢复、幂等与评估门槛 V01–V10。

## 评估结果和它盖不住的事

`aiops eval` 在 2026-11-10 复核 V01–V10 全部通过。这是功能门槛，不是稳定性证明：

- 场景运行次数：f01 坏发布 13 次、f02 OOM 5 次、f03 Redis 延迟 4 次、a01 Pod 重建 3 次。
- 最弱的一处：f03 在旧预算下一次都没对（1/4），预算按实测重定并改提示词后连续 3 次都对；旧的失败记录保留在清单里。
- a01 只验证重建动作，不参与调查评分。
- 门槛是「每个核心场景至少 3 次有效运行、至少 1 次正确」。
- 历史 76 个调查的原始观测已丢失，只有清单登记的那次有仓库内副本。

每条结论的证据位置见[主张—证据矩阵](docs/claims.md)。

## 自己核对

```sh
uv sync --frozen && make check-all   # 离线可跑：231 passed, 116 skipped（需要数据库的跳过）
uv run aiops db migrate && uv run aiops eval   # 需要实验 PostgreSQL，输出 V01–V10
```

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

LangGraph、PostgreSQL checkpoint、模型客户端与 Kubernetes、HTTP 依赖已锁定；pgvector 与向量检索按后续里程碑接入。旧锁文件和旧 `.env` 保留在归档目录，新项目不读取归档配置。旧 `.env` 在迁移前已被 Git 跟踪，本次没有清理 Git 历史；其中三个值（`POSTGRES_PASSWORD`、`JWT_SECRET_KEY`、`INITIAL_ADMIN_PASSWORD`）经确认已不在任何环境使用，也不需要轮换。
