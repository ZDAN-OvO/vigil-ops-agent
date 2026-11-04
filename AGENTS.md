# Vigil AIOps 重构

- 单项目直接位于仓库根目录，源码在 `src/aiops/`，测试在 `tests/`；使用简体中文文档/注释/提交信息，英文标识符；Linux + uv。
- `deprecated/` 是旧项目的原样归档。新代码、测试、配置和依赖不得导入或自动加载归档内容；旧 `.env` 保持本地，不加入提交。
- 修改架构或跨模块接口前，先读 [架构](docs/architecture.md) 与 [契约](docs/contracts.md)；阶段验收见 [实施计划](docs/milestone.md)。未经确认的建议不得升级为用户已定决策。
- 依赖能解决的通用能力优先复用；LangGraph 承担编排、持久化和审批恢复，业务快照与补偿动作独立实现。
- 配置通过 Pydantic Settings；日志使用 Loguru。新脚本使用 shell/Python，不新增 Windows 启动脚本。
- 修改后运行根目录 `make check-all`。涉及基础设施或写操作时，还须补充对应的真实环境证据；mock 通过不代表集群行为通过。
- 并行写入使用独立 worktree，并先对齐契约。目标分支走 PR；合并、auto-merge、破坏性清理需要用户明确授权。

## Agent skills

### Issue tracker

Track issues and specs in GitHub Issues for `ZDAN-OvO/vigil-ops-agent`.
See [Issue tracker](docs/agents/issue-tracker.md) for operations and authorization rules.

### Triage labels

Use the five default triage labels mapped in [Triage labels](docs/agents/triage-labels.md).

### Domain docs

Use a single context with root `GLOSSARY.md` and `docs/adr/`.
See [Domain docs](docs/agents/domain.md) before exploring domain terms or decisions.
