# Vigil AIOps

面向中小企业和个人开发者的 Kubernetes 故障调查与受控处置。告警或手动请求触发调查，Agent 按排查方法（Skill）在有界预算内取证并维护假设，程序按规则或人工审批执行限定动作，再用真实业务请求验证恢复。

- **调查**：LangChain `create_agent` 主循环，按需加载 Skill，可以委派只读子调查。
- **处置**：只有 `rollout_undo` 与 `restart_pod` 两种动作；先做前置检查、风险分级与审批，执行后按业务请求验证。异常结果会停止后续动作，并生成人工待办。
- **记录**：事件、调查、观测、假设与证据、提案、审批和执行都存进 PostgreSQL；审批可以跨进程恢复，重放不会重复写集群。

模块边界与架构约束见[全局架构](ARCHITECTURE.md)，当前未完成事项见[阶段计划](docs/plan.md)，历史验收结果见[运行记录汇总](faults/runs/skill-runtime-acceptance.md)。

## 技术栈

Linux + uv，Python 3.12–3.13；LangChain `create_agent` 负责调查工具循环，LangGraph 负责 checkpoint 和审批恢复，PostgreSQL 保存业务状态。Kubernetes Python client 与 HTTP 接入集群和 Prometheus；Pydantic Settings 管理配置，Loguru 记录日志，Typer 与 Starlette 分别提供 CLI 和 webhook。模型通过现有网关的 OpenAI 兼容端点接入。

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

模块依赖与动作边界见[架构](ARCHITECTURE.md)，术语见[词汇表](GLOSSARY.md)。搭建和复位集群见[实验环境](env/README.md)，注入与判定故障见[故障演练](faults/README.md)。

源码在 [src/aiops/](src/aiops/)，排查方法在 [skills/](skills/)。旧项目原样归档在 [deprecated/](deprecated/README.md)，新代码不导入、不读取归档内容。
