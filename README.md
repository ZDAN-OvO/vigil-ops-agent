# Vigil AIOps

面向中小企业和个人开发者的 Kubernetes 故障调查与受控处置。在本机页面描述故障、补充线索、查看证据，并人工审批 Deployment 回滚。Vigil 用首页探针验证恢复，告警调查在后台只读取证。

- **调查**：按症状选择排查方法，取证并维护假设，可以委派只读子调查。
- **处置**：只有 `rollout_undo` 与 `restart_pod` 两种动作；先做前置检查、风险分级与审批，执行后按业务请求验证。异常结果会停止后续动作，并生成人工待办。
- **记录**：事件、调查、观测、假设与证据、提案、审批和执行都存进 PostgreSQL；审批可以跨进程恢复，重放不会重复写集群。
- **对照评估**：同一故障集上跑 Agent、通用 Agent 与规则基线三条臂，按预注册口径判定；见[方法与门槛](docs/adr/0013-comparative-evaluation.md)。

## 快速开始

运行要求：Linux、uv、Python 3.12–3.13、Node.js 和 pnpm。

```sh
make init
cp .env.example .env   # 首次配置时填写实验环境的连接信息
uv run --frozen aiops dev db migrate
make web-build
uv run --frozen aiops doctor
uv run --frozen aiops serve
```

打开终端显示的本机地址，默认是 `http://127.0.0.1:8080/`。要运行开发命令或对照评估，先停止服务，再用 `aiops dev --help` 查看入口。

- [操作手册](docs/USAGE.md)：配置、调查、审批与排障。
- [实验环境](env/README.md)：搭建集群与观测组件。
- [故障演练](faults/README.md)：注入、验收与复位。
- [阶段计划](docs/plan.md)：当前未完成事项与验收条件。
