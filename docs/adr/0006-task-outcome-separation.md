# ADR-0006：任务运行状态与诊断结果分开

- 状态：已采纳（2026-11-11）
- 相关：[契约](../contracts.md)、[MVP](../mvp.md)、[决策 D28](../decisions.md)

## 背景

旧调查只有 `investigations.status` 一个字段，同时承担“诊断是否得出结论”和“流程是否还在跑”两种含义。接入 LangChain 后，一次调查可能在等待审批、被取消、预算耗尽、模型失败或业务已经恢复；这些都不是同一个维度。继续用 `completed/failed` 会让“等待人工”看起来像失败，也会让“业务已恢复但没形成根因候选”被误报为成功诊断。

用户要求：旧 JSON 的诊断 `status` 含义保持不变；新任务的运行状态放入 `data.task`，停止原因使用稳定代码。

## 决定

1. 保留 `investigations.status` 作为诊断结果：`completed` 表示形成结论，`failed`/`inconclusive` 等旧值语义不变。它不表示流程是否结束，也不表示业务是否恢复。
2. 新增 `investigation_tasks` 1:1 记录运行状态：
   - `phase`：`running` / `waiting_approval` / `terminal`；
   - `outcome`：`recovered` / `diagnosis_only` / `inconclusive` / `needs_human` / `aborted`；
   - `termination_code`：稳定代码，如 `business_recovered`、`no_applicable_action`、`insufficient_evidence`、`no_progress`、`action_no_improvement`、`action_worsened`、`action_unknown`、`policy_denied`、`budget_exhausted`、`cancelled`、`model_failure`、`store_failure`、`invalid_report`、`interrupted`、`runtime_error`。
3. 数据库检查约束保证：终态必须有 outcome 和 termination_code；非终态不得有 outcome；`waiting_approval` 必须绑定提案。
4. CLI 的旧字段保持原含义；新字段只出现在 `data.task`。退出码由任务结果决定：恢复或仅诊断成功为 0，等待人工/需人工为 3，策略拒绝为 4，信息不足为 5，其余失败为 1。
5. 业务恢复单独记录 `recovered_at`，不把“报告里写了恢复”当作恢复证据；恢复归因仍由执行器的业务与平台断言决定。

## 后果

- 同一份报告可以表达“业务已恢复，但根因未确认”和“根因已确认，但没有适用动作”，不会互相覆盖。
- 旧调查没有 task 行，读取时返回 `None`，报告与评估按旧语义处理；新调查必须创建 task。
- 终态、等待、取消和写意图分别可查，恢复流程可以先判断是否 terminal、是否 waiting、是否存在未完成的写意图，再决定是否允许 resume。
- 新增稳定代码属于契约变更，需要同步 `contracts.py`、CLI 文档与评估断言；不能临时用错误字符串代替。
