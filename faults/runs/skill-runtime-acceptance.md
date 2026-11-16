# Skill、工具与委派调查运行时验收

设计见 [ADR-0007](../../docs/adr/0007-skill-tool-subagent-runtime.md)。状态：**验收通过**；各项历史结果见下表。

| 验收项 | 门槛 | 结果 | 记录 |
|---|---|---|---|
| 坏发布 | 至少 3 次有效运行，至少 1 次调查正确，至少 1 次业务恢复 | 4 次有效运行，调查正确 3 次，恢复 2 次 | [运行记录](../f01-bad-release/runs/2026-11-13.md) |
| 持续 OOM | 同上 | 3 次有效运行，诊断全部正确，恢复 2 次 | [运行记录](../f02-memory-limit-oom/runs/2026-11-13.md) |
| Redis 访问链路延迟 | 至少 3 次有效运行，至少 1 次调查正确，正确停止且没有写操作 | 3 次有效运行，2 次定位正确，全部 0 提案、0 写操作 | [运行记录](../f03-redis-latency/runs/2026-11-13.md) |
| Pod 重建 | 至少 1 次由 Agent 自主执行 `restart_pod`，Pod UID 改变、模板不变 | 5 次尝试中成功 1 次；2026-11-17 补充 3 次均成功 | [2026-11-13](../a01-pod-rebuild/runs/2026-11-13.md)、[2026-11-17](../a01-pod-rebuild/runs/2026-11-17.md) |
| 审批跨进程恢复 | 至少 1 条真实记录 | 4 个提案在新进程中从 checkpoint 恢复并执行 | 见下文“跨场景验收” |
| 重放不重复写入 | 至少 1 条真实记录 | 对已成功提案再次批准，返回 `not_pending`，执行计数仍为 1 | 见下文“跨场景验收” |
| 委派调查 | 至少 1 次运行使用委派，能查到目标、所用 Skill 和返回的观测 | 委派 `dlg-cb09062b5f63` 成功，子任务 Skill 与 22 条返回观测均可按委派 ID 回查，没有写操作 | [运行记录](../f01-bad-release/runs/2026-11-13-delegation.md) |
| 追踪平台 | 默认关闭时业务行为不变，启用后能接入 | 自动化测试覆盖；按用户决定，免除真实 trace 验证 | [tests/test_tracing.py](../../tests/test_tracing.py) |
| 自动化检查 | `make check-all` 通过 | PR-B：120 passed、160 skipped（未配置数据库），CI 通过 | [PR-B #8](https://github.com/ZDAN-OvO/vigil-ops-agent/pull/8) · [checks](https://github.com/ZDAN-OvO/vigil-ops-agent/actions/runs/43678390320/job/132703462697) |

## 跨场景验收

- **审批跨进程恢复**：`aiops investigate` 停在 `waiting_approval` 后退出，再由新进程执行 `aiops approve <提案 ID>`，从 PostgreSQL checkpoint 恢复并执行。坏发布的 `act-dd85c844dc42`、`act-212852a7432f` 和持续 OOM 的 `act-a9899a135653`、`act-774d7502d9be` 都走了这条路径。
- **重放不重复写入**：对已成功的 `act-212852a7432f` 再次执行 `aiops approve`，返回 `not_pending`（提案状态 `succeeded`），`action_attempts` 计数仍为 1。
- **委派调查**：`inv-dd6dd82fc47d` 委派 `dlg-cb09062b5f63`，子任务 Skill 为 `hypothesis-driven-investigation`，26 条观测中 22 条带该委派 ID，且都来自只读工具调用；提案与执行记录均为 0。主调查本身未收敛。详见[运行记录](../f01-bad-release/runs/2026-11-13-delegation.md)。

## 已交付能力

| 能力 | 主要依据 |
|---|---|
| 坏发布从告警到业务恢复 | [坏发布场景](../f01-bad-release/README.md) |
| 审批持久化，跨进程恢复，过期或资源漂移时不沿用授权 | [操作手册](../../docs/USAGE.md#4-命令手册)、[ADR-0009](../../docs/adr/0009-risk-verification-and-stop.md) |
| 持续 OOM 的调查与处置适用性判断 | [持续 OOM 场景](../f02-memory-limit-oom/README.md) |
| Redis 访问链路延迟的调查与正确停止 | [Redis 访问链路延迟场景](../f03-redis-latency/README.md) |
| Pod 重建的适用性判断与业务验证 | [Pod 重建场景](../a01-pod-rebuild/README.md) |
| 结构化 runbook 的查询与引用 | `aiops knowledge` |
| 异常执行结果的停止与人工待办 | `aiops followups` |
| LangChain `create_agent` 调查主循环 | [ADR-0005](../../docs/adr/0005-langchain-single-agent-loop.md) |
| 任务运行状态与诊断结果分开记录 | [ADR-0006](../../docs/adr/0006-task-outcome-separation.md) |
| Skill、工具注册表与只读委派调查 | [ADR-0007](../../docs/adr/0007-skill-tool-subagent-runtime.md) |

通用判定标准见[验收规则](../README.md#验收规则)。Pod 重建手动入口的后续运行结果独立保存在 [2026-11-15 记录](../a01-pod-rebuild/runs/2026-11-15.md)；未完成事项见[当前阶段计划](../../docs/plan.md)。
