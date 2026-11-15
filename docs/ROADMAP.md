# 路线图与验收

本文是项目进度、验收门槛和结果的唯一来源。其他文档不写进度说明，只链接到这里。

## 当前阶段：Skill、工具与委派调查运行时

设计见 [ADR-0007](adr/0007-skill-tool-subagent-runtime.md)。状态：**已通过**（2026-11-14）。

| 验收项 | 门槛 | 结果 | 记录 |
|---|---|---|---|
| 坏发布 | 至少 3 次有效运行，至少 1 次调查正确，至少 1 次业务恢复 | 4 次有效运行，调查正确 3 次，恢复 2 次 | [运行记录](../faults/f01-bad-release/runs/2026-11-13.md) |
| 持续 OOM | 同上 | 3 次有效运行，诊断全部正确，恢复 2 次 | [运行记录](../faults/f02-memory-limit-oom/runs/2026-11-13.md) |
| Redis 访问链路延迟 | 至少 3 次有效运行，至少 1 次调查正确，正确停止且没有写操作 | 3 次有效运行，2 次定位正确，全部 0 提案、0 写操作 | [运行记录](../faults/f03-redis-latency/runs/2026-11-13.md) |
| Pod 重建 | 至少 1 次由 Agent 自主执行 `restart_pod`，Pod UID 改变、模板不变 | 5 次尝试中成功 1 次 | [运行记录](../faults/a01-pod-rebuild/runs/2026-11-13.md) |
| 审批跨进程恢复 | 至少 1 条真实记录 | 4 个提案在新进程中从 checkpoint 恢复并执行 | 见下文“跨场景验收” |
| 重放不重复写入 | 至少 1 条真实记录 | 对已成功提案再次批准，返回 `not_pending`，执行计数仍为 1 | 见下文“跨场景验收” |
| 委派调查 | 至少 1 次运行使用委派，能查到目标、所用 Skill 和返回的观测 | `inv-dd6dd82fc47d` 的委派 `dlg-cb09062b5f63` 成功，返回 22 条带 `delegation_id` 的观测 | 见下文“跨场景验收” |
| 追踪平台 | 默认关闭时业务行为不变，启用后能接入 | 自动化测试覆盖；按用户决定，免除真实 trace 验证 | `tests/test_tracing.py` |
| 自动化检查 | `make check-all` 通过 | PR-B：91 passed、160 skipped（未配置数据库），CI 通过 | [PR-B #8](https://github.com/ZDAN-OvO/vigil-ops-agent/pull/8) · [checks](https://github.com/ZDAN-OvO/vigil-ops-agent/actions/runs/43675597662/job/132694924401) |

### 跨场景验收

- **审批跨进程恢复**：`aiops investigate` 停在 `waiting_approval` 后退出，再由新进程执行 `aiops approve <提案 ID>`，从 PostgreSQL checkpoint 恢复并执行。坏发布的 `act-dd85c844dc42`、`act-212852a7432f` 和持续 OOM 的 `act-a9899a135653`、`act-774d7502d9be` 都走了这条路径。
- **重放不重复写入**：对已成功的 `act-212852a7432f` 再次执行 `aiops approve`，返回 `not_pending`（提案状态 `succeeded`），`action_attempts` 计数仍为 1。
- **委派调查**：`inv-dd6dd82fc47d` 的主调查未收敛，但委派 `dlg-cb09062b5f63` 成功，所用 Skill 为 `hypothesis-driven-investigation`，返回的 22 条观测都带有 `delegation_id`。

### 遗留问题

- **Pod 重建的手动调查成功率低**：手动入口不会自动加载 `pod-rebuild` Skill，因为它只按 `PodCrashLooping`、`PodNotReady` 告警名触发。修复方案是新增 `metadata.vigil-auto-load-services` 按服务名自动加载，**待用户确认**。根因分析见[运行记录](../faults/a01-pod-rebuild/runs/2026-11-13.md#连续失败的根因)。
- **相对规则基线和通用 Agent 的收益**：尚未证明，在对照评估重建后验证（[ADR-0008](adr/0008-product-scope-and-lab.md)）。

## 验收规则

以下规则适用于所有真实环境验收。

- **有效运行**：健康基线成立、故障核验通过、注入与观测记录齐全，才算有效运行。只有独立前提失败（例如注入未生效、基线不健康）才能标为无效，并且要保留理由。
- **分母**：所有有效运行都计入分母。模型失败、预算耗尽、错误判断都不能从分母中剔除；失败运行不会被后来的成功运行覆盖。
- **调查是否正确**：按各场景 README 中的“可接受结论”和“必需证据”人工核对，并把依据写进运行记录。
- **恢复归属**：Agent、操作者、控制器和实验脚本的动作分开记录。操作者复位和 cleanup 不算 Agent 恢复；动作前业务已经恢复，也不算 Agent 恢复。
- **证据**：截图和录屏贴在 PR 评论里，覆盖初始、关键中间和最终状态，不提交到仓库，也不放进 PR 正文。运行结论写入 `faults/<场景>/runs/<日期>.md`，引用调查 ID 与提案 ID。

## 停止条件

- 同一场景连续 3 次有效运行失败：暂停，给出根因分析与修复方案，等用户确认后继续。
- 预算成为失败主因：按 [ADR-0011](adr/0011-investigation-budget.md) 的方法重测，等用户确认后再修改默认值。
- 删除表、清理集群资源、强制推送、合并 PR：先取得用户授权。

## 已交付能力

| 能力 | 主要依据 |
|---|---|
| 坏发布从告警到业务恢复 | [坏发布场景](../faults/f01-bad-release/README.md) |
| 审批持久化，跨进程恢复，过期或资源漂移时不沿用授权 | [设计文档](DESIGN.md#57-动作请求与风险决策) |
| 持续 OOM 的调查与处置适用性判断 | [持续 OOM 场景](../faults/f02-memory-limit-oom/README.md) |
| Redis 访问链路延迟的调查与正确停止 | [Redis 访问链路延迟场景](../faults/f03-redis-latency/README.md) |
| Pod 重建的适用性判断与业务验证 | [Pod 重建场景](../faults/a01-pod-rebuild/README.md) |
| 结构化 runbook 的查询与引用 | `aiops knowledge` |
| 异常执行结果的停止与人工待办 | `aiops followups` |
| LangChain `create_agent` 调查主循环 | [ADR-0005](adr/0005-langchain-single-agent-loop.md) |
| 任务运行状态与诊断结果分开记录 | [ADR-0006](adr/0006-task-outcome-separation.md) |
| Skill、工具注册表与只读委派调查 | [ADR-0007](adr/0007-skill-tool-subagent-runtime.md) |

## 后续能力

按顺序推进。改变优先级时要写明依据。

1. **处置可靠性**：执行中断后核实真实结果；处置恶化后做限定补偿；受控扩缩容。
2. **对话与持续运行**：前端与持续对话（包括运行中追加提示）；常驻告警任务在重启后恢复；定时巡检。
3. **对照评估重建**：在新数据模型上重建场景评分，与固定规则和通用 Agent 做同条件对照。
4. **知识扩展**：经人工确认的历史事故；文本 PDF 的完整生命周期与向量检索。
5. **完整交付**：跨指标、日志、trace 追溯；四类 12 个故障场景；第三方复现。

每项能力开始前，先在本文写出验收门槛，再拆成 GitHub Issues。
