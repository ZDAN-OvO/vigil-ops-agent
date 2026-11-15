# Vigil AIOps

面向中小企业和个人开发者的 Kubernetes 故障调查与受控处置。告警或手动请求触发调查，Agent 按排查方法（Skill）在有界预算内取证并维护假设，程序按规则或人工审批执行限定动作，再用真实业务请求验证恢复。

- **调查**：LangChain `create_agent` 主循环，按需加载 Skill，可以委派只读子调查。
- **处置**：只有 `rollout_undo` 与 `restart_pod` 两种动作；先做前置检查、风险分级与审批，执行后按业务请求验证。异常结果会停止后续动作，并生成人工待办。
- **记录**：事件、调查、观测、假设与证据、提案、审批和执行都存进 PostgreSQL；审批可以跨进程恢复，重放不会重复写集群。

当前进度和验收结果见[路线图](docs/ROADMAP.md)。

## 快速开始

```sh
make init                                   # 安装锁定依赖与开发工具
cp .env.example .env                        # 按需填写 VIGIL_ 配置
make check-all                              # 格式、lint 与测试；无数据库时相关用例跳过
uv run --frozen aiops --help
uv run --frozen aiops db migrate            # 需要 VIGIL_DATABASE_URL
uv run --frozen aiops investigate --service frontend --symptoms "首页 500"
```

调查需要实验集群、Prometheus、PostgreSQL 和模型网关，完整前置条件与演练见[操作手册](docs/USAGE.md)。

## 文档

| 想了解什么 | 文档 |
|---|---|
| 怎么安装、配置、操作 | [操作手册](docs/USAGE.md) |
| 系统怎样设计、必须遵守哪些规则 | [设计文档](docs/DESIGN.md) |
| 单项决策的背景与备选方案 | [ADR](docs/adr/) |
| 当前进度、验收门槛与结果 | [路线图](docs/ROADMAP.md) |
| 术语的含义 | [词汇表](GLOSSARY.md) |
| 实验环境怎么搭建和复位 | [实验环境](env/README.md) |
| 故障场景怎样注入和判定 | [故障目录](faults/README.md) |

源码在 [src/aiops/](src/aiops/)，排查方法在 [skills/](skills/)。旧项目原样归档在 [deprecated/](deprecated/README.md)，新代码不导入、不读取归档内容。
