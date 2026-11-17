# Vigil AIOps 重构

- 单项目直接位于仓库根目录，源码在 `src/aiops/`，测试在 `tests/`；使用简体中文文档/注释/提交信息，英文标识符；Linux + uv。
- `deprecated/` 是旧项目的原样归档。新代码、测试、配置和依赖不得导入或自动加载归档内容；旧 `.env` 保持本地，不加入提交。
- 修改架构或跨模块接口前，先读 [全局架构](ARCHITECTURE.md)和受影响的 [ADR](docs/adr/)。用户拥有产品与技术设计权；未经确认的建议不得升级为已定决策。
- 文档分工：架构与模块边界写在 `ARCHITECTURE.md`，决策写在 `docs/adr/`，操作写在 `docs/USAGE.md`，场景机制与运行记录写在 `faults/`，当前阶段任务写在 `docs/plan.md`，术语写在 `GLOSSARY.md`。每条规则保持单一权威来源，其他位置用摘要和链接导航；现行文档有误时直接修改；正文用场景名称，不用目录编号。阶段计划完成后清理任务，保留运行证据。
- 依赖能解决的通用能力优先复用；LangGraph 承担编排、持久化和审批恢复，业务快照与补偿动作独立实现。
- 配置通过 Pydantic Settings；日志使用 Loguru。新脚本使用 shell/Python，不新增 Windows 启动脚本。
- 修改后运行根目录 `make check-all`。涉及基础设施或写操作时，还须补充对应的真实环境证据；mock 通过不代表集群行为通过。
- PR 中展示的证据一律用截图或录屏，直接上传到 PR 评论，不放进 PR 正文，也不提交到仓库；命令输出、日志和数据库记录只能作为补充。真实环境证据要覆盖初始、关键中间状态和最终状态。本地生成的截图放在 `.evidence/`（已忽略），运行结论写入 `faults/<场景>/runs/<日期>.md`，引用调查 ID 与提案 ID。
- 并行写入使用独立 worktree，并先对齐契约。目标分支走 PR；合并、auto-merge、破坏性清理需要用户明确授权。

## 协作规则

- 需求、任务与讨论用 `ZDAN-OvO/vigil-ops-agent` 的 GitHub Issues 跟踪；创建、评论或关闭 Issue 仍须当前任务授权。
- 分诊使用五个默认标签：`needs-triage`、`needs-info`、`ready-for-agent`、`ready-for-human`、`wontfix`。
- 领域术语用根 `GLOSSARY.md`，重要决策用 `docs/adr/`；发现两者冲突时先指出冲突，再决定是否重开决策。
