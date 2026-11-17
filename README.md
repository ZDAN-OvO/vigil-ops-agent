# Vigil AIOps

面向中小企业和个人开发者的 Kubernetes 故障调查与受控处置。告警或手动请求触发调查，Agent 按排查方法（Skill）在有界预算内取证并维护假设，程序按规则或人工审批执行限定动作，再用真实业务请求验证恢复。

- **调查**：按症状选择排查方法，取证并维护假设，可以委派只读子调查。
- **处置**：只有 `rollout_undo` 与 `restart_pod` 两种动作；先做前置检查、风险分级与审批，执行后按业务请求验证。异常结果会停止后续动作，并生成人工待办。
- **记录**：事件、调查、观测、假设与证据、提案、审批和执行都存进 PostgreSQL；审批可以跨进程恢复，重放不会重复写集群。

## 快速开始

运行要求：Linux、uv、Python 3.12–3.13。

```sh
make init                                   # 安装锁定依赖与开发工具
cp .env.example .env                        # 按需填写 VIGIL_ 配置
make check-all                              # 格式、lint 与测试；无数据库时相关用例跳过
uv run --frozen aiops --help
uv run --frozen aiops db migrate            # 需要 VIGIL_DATABASE_URL
uv run --frozen aiops investigate --service frontend --symptoms "首页 500"
```

调查需要实验集群、Prometheus、PostgreSQL 和模型网关。

- [操作手册](docs/USAGE.md)：配置、调查、审批与排障。
- [实验环境](env/README.md)：搭建集群与观测组件。
- [故障演练](faults/README.md)：注入、验收与复位。
