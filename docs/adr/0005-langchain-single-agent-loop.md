# ADR-0005：用 LangChain 单 Agent 替换手写假设循环

- 状态：已采纳（2026-11-11）
- 相关：[架构](../architecture.md)、[契约](../contracts.md)、[决策 D27](../decisions.md)、[ADR-0002](0002-langgraph-orchestration.md)

## 背景

旧调查循环在 `src/aiops/investigation/runtime.py` 里手写 plan → collect → assess → propose：提示词、JSON 解析、工具批次、预算扣减、无进展判断和终态报告都在自定义状态机里。它能跑通 MVP，但每次调整提示词或工具协议都要同时改解析器和状态机；模型重试、结构纠错、审批中断这些通用能力也被重复实现。用户要求把主循环换成 LangChain 1.x 的 `create_agent`，同时保留既有 LangGraph 持久化、工具适配器、风险策略、执行器和 CLI/webhook 入口。

约束是：外部行为不能退化；旧调查、旧提案与旧评估记录要能继续读取；审批仍要跨进程恢复；写操作不能因 checkpoint 重放而执行两次；注入真值和场景文件不能进入模型上下文。

## 决定

1. 调查主循环只使用 `langchain.agents.create_agent`，不再保留手写循环。模型、工具集合和终态协议在运行时组装；`src/aiops/investigation/runtime.py`、`prompts.py`、`schemas.py` 删除。
2. 工具面收敛为 11 个：8 个只读取证工具、`manage_hypotheses`、`assess_evidence`、`request_action`。工具通过 `ToolRuntime` 获得固定运行上下文，只暴露集群、允许命名空间、预算和 Store，不接受模型传入权限或任意命令。
3. 通用控制放在三个中间件里，而不是散落在提示词：
   - `ActionBatchMiddleware`：写动作必须单独成批；同一批混入只读调用时整批拒绝；业务恢复后拒绝新动作。
   - `BudgetMiddleware`：每次模型尝试先扣预算，单次调用按剩余时间限时，只对瞬态错误有限重试，认证/参数错误不重试。
   - `ProgressMiddleware`：无新活动或只有无效工具调用时累计无进展，3 轮后停止。
4. 终态用 `ToolStrategy(ReportDraft)` 约束为结构化报告，再做客户端引用校验；结构错误只允许一次纠错，避免把“重新生成到合法”当作无限预算。
5. 审批在 `request_action` 工具内用 `interrupt()` 暂停，用户决定通过 `Command(resume=...)` 回到同一图；工具调用前先写 `tool_requests` 账本，重放时按 `(investigation_id, tool_call_id)` 短路，保证外部写不会执行两次。
6. 业务副作用仍由执行器、风险策略和审批表负责；checkpoint 只保存流程进度，不替代资源快照、幂等键或审计。

## 后果

- 旧调查循环和旧提示词删除，旧调查记录、旧提案、审批与报告仍可读；规则基线保留，作为同条件对照。
- 新增 `investigation_tasks`、`observations`、`hypothesis_evidence`、`tool_requests` 四类业务记录；`action_proposals` 增加 purpose、prediction 和观察条件。
- 预算从“工具 28 / 模型 10 / 墙钟 600 秒”扩展为调查预算加动作预算（默认 450 秒）；动作执行时间不再挤占调查时间，但每次写前要预留完整适配器上界和一次反馈模型调用。
- 工具在本期按批次顺序串行执行，不引入并行调度；未来若并行，需要先证明账本、预算和写动作互斥仍然成立。
- `interrupt()` 只在工具内部使用；恢复时 LangGraph 会从工具开头重跑，因此任何外部副作用都必须先查账本。这个语义已用真实 PG checkpoint 实验验证。
