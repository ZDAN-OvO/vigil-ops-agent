# Vigil AIOps

Vigil 在终端和本机网页里调查 Kubernetes 故障。它用集群和监控数据回答问题，按需查看内置排查 Skill，并把会话、证据、卡片和执行结果保存在 PostgreSQL。

- 在调查中补充线索。停在审批卡片时，仍可另问只读问题。
- AlertManager 告警按组进入会话。可以在网页查看进展，也可以在终端继续排查。
- 核对卡片后，再批准回滚 Deployment 或重建 Pod。Vigil 会记录发送和验证结果，供接手或人工核实。

## 快速开始

使用 Linux、uv 和 Python 3.12–3.13。准备好 Kubernetes、Prometheus、PostgreSQL 和本机模型网关后，在源码目录执行：

```sh
uv sync --frozen --group dev
cp -n .env.example .env
# 填好本机配置后运行
uv run --frozen vigil admin migrate
uv run --frozen vigil admin doctor
uv run --frozen vigil "checkoutservice 现在健康吗？"
```

使用 Node.js 26 和 npm 运行 `make web-build` 构建源码版网页。随后在交互终端启动 `uv run --frozen vigil serve`，打开它打印的一次性链接即可在网页提问和审批。命令行参数、安装版配置和旧库切换见操作手册。

- [操作手册](docs/USAGE.md)
- [实验环境](env/README.md)
- [故障演练](faults/README.md)
