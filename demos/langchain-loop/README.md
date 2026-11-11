# LangChain 单 Agent 主循环：真实运行与验收

2026-11-11 在本机 k3d 实验集群执行。目标是把调查主循环从 `src/aiops/investigation/runtime.py` 的手写状态机替换为 LangChain 1.x `create_agent`，同时保留 LangGraph checkpoint、PostgreSQL 业务记录、动作执行器、风险策略和 CLI/webhook。设计取舍见 [ADR-0005](../../docs/adr/0005-langchain-single-agent-loop.md) 与 [ADR-0006](../../docs/adr/0006-task-outcome-separation.md)。

本轮不追溯改写 `demos/ms01`–`ms08` 的旧评分；下面的运行是新主循环的独立证据。失败运行保留，不用成功运行覆盖。

## 1. 运行清单

| 场景 | 运行 | 调查 | 提案 | 任务结果 | 调查结论 | 关键结果 | 原始输出 |
|---|---|---|---|---|---|---|---|
| F01 坏发布 | 成功 1 | `inv-a42bb167ef79` | `act-a04ad9d95064` | recovered / business_recovered | recovered | 回退到 revision 69，home 200、probe 1 | `f01-05-*` |
| F01 坏发布 | 成功 2 | `inv-113347187e89` | `act-84a9285ffe31` | recovered / business_recovered | recovered | 调查 101.4s、动作 122.3s，17 观测/17 工具/15 关系 | `f01-06-*` |
| F01 坏发布 | 成功 3 | `inv-8717f8e23b0d` | `act-c2f1258016e0` | recovered / business_recovered | recovered | 调查 104.1s、动作 124.4s，19 观测/19 工具/13 关系 | `f01-07-*` |
| F01 错误初始线索 | 补充 | `inv-835c8722164b` | `act-5ec078cd3671` | recovered / business_recovered | recovered | 以 `--service frontend` 开始，反驳 frontend 假设，定位 productcatalogservice canary，回退 revision 77 | `f01-08-wrong-lead-*` |
| F02 持续 OOM | 成功 1 | `inv-5f3d078e2391` | `act-e8e24a2c8c44` | recovered / business_recovered | inconclusive | 回退 32→31、128Mi；工具预算耗尽导致总结未完成 | `f02-01-*` |
| F02 持续 OOM | 成功 2 | `inv-1a7b82fae9ef` | `act-c5659c03ac57` | recovered / business_recovered | inconclusive | 135s 观察窗；报告两次结构失败（InvalidReport），确定性总结 | `f02-03-*` |
| F02 持续 OOM | 成功 3 | `inv-2141db2fe0d9` | `act-14ff80e771a4` | recovered / business_recovered | recovered | 先请求 `restart_pod` 被 `not_discardable` 拒绝，再回退 40→39；10 观测/10 工具/13 关系 | `f02-05-*` |
| F03 Redis 延迟 | 停止 1 | `inv-14e948d696fd` | 无 | inconclusive / insufficient_evidence | inconclusive | 27 观测/27 工具/**0 关系**；人工待办已指向代理与 selector | `f03-01-*` |
| F03 Redis 延迟 | 停止 2 | `inv-52edca3d91a8` | 无 | inconclusive / insufficient_evidence | inconclusive | 22 观测/28 工具/17 关系；未形成可提交结论 | 数据库快照 |
| F03 Redis 延迟 | 停止 3 | `inv-ce0a28085a5a` | 无 | inconclusive / insufficient_evidence | inconclusive | 18 观测/24 工具/12 关系；未形成可提交结论 | 数据库快照 |
| F03 Redis 延迟 | 停止 4 | `inv-cd591aeda0a3` | 无 | inconclusive / insufficient_evidence | inconclusive | 21 观测/21 工具/11 关系；支持 selector/代理插入假设 | `f03-02-*` |
| F03 Redis 延迟 | 停止 5 | `inv-acf954a8d14b` | 无 | inconclusive / insufficient_evidence | inconclusive | 23 观测/23 工具/16 关系；支持代理插入，未确认代理内部机制 | `f03-03-*` |
| A01 Pod 重建 | 补充 | `inv-988f6a59c808` | `act-de5b0f7c1791` | recovered / business_recovered | inconclusive | 模型自主诊断性 `restart_pod`；新 UID、模板不变、/work 恢复；竞争假设未强行确认根因 | `a01-01-*` |

所有调查 ID 可在实验 PostgreSQL 的 `investigations`、`investigation_tasks`、`action_proposals`、`action_attempts` 表复查；紧凑快照见 `outputs/final-db-state.txt`。

### 有效运行分母

健康基线成立、故障核验通过、注入与观测记录齐全的运行全部计入分母，失败运行不从分母剔除；恢复、正确调查、停止三类结果分别计数：

| 场景 | 有效运行 | 业务恢复 | 正确调查 | 保留在分母的失败/降级 |
|---|---|---|---|---|
| F01 | 8：`inv-28d87355496a`、`inv-d82bf5cb0ea4`、`inv-d47e19766e27`、`inv-120e286e8bcc`、`inv-a42bb167ef79`、`inv-113347187e89`、`inv-8717f8e23b0d`、`inv-835c8722164b` | 5（`inv-28d8` 虽 `aborted/budget_exhausted`，prediction 实际 improved 且业务恢复；`inv-a42b`、`inv-1133`、`inv-8717`、`inv-835c`） | 5（成功 1–3、错误线索、`inv-d47e` 诊断正确但动作被 duplicate 拒绝） | `inv-d82b` 审批后未写；`inv-120e` invalid_report |
| F02 | 5：`inv-5f3d078e2391`、`inv-15183fc1a260`、`inv-1a7b82fae9ef`、`inv-18f3d92d1d77`、`inv-2141db2fe0d9` | 3（`inv-5f3d`、`inv-1a7b`、`inv-2141`） | 3（`inv-1518`、`inv-18f3`、`inv-2141` 确认 OOM 与限额回退；`inv-5f3d`/`inv-1a7b` 结论被预算/结构问题降级） | `inv-1518`、`inv-18f3` 为真实 `no_improvement`，保留为假阴性并附修复记录 |
| F03 | 5：`inv-14e948d696fd`、`inv-52edca3d91a8`、`inv-ce0a28085a5a`、`inv-cd591aeda0a3`、`inv-acf954a8d14b` | 0（cleanup 是操作者动作，不算 Agent 恢复） | 2（`inv-cd59`、`inv-acf9` 关系支持 selector/代理插入；`inv-14e9` 无关系保留） | 五次均正确停止且无写；`inv-14e9` 无关系、`inv-52ed`/`inv-ce0a` 未形成结论 |
| A01 | 1：`inv-988f6a59c808` | 1（新 UID、模板不变、/work 恢复） | 0（本地状态与坏配置两个竞争假设，未强行确认根因） | 调查 `inconclusive` 按 A01 场景卡不算错，单列 |

F01 #1–#4 是同一主循环的早期失败，保留原因见第 2 节；F02 的两次 `no_improvement` 是观察窗与尾段样本的真实假阴性，已分别修复并重跑成功。

## 2. 失败与假阴性记录

| 记录 | 现象 | 根因与修复 |
|---|---|---|
| F01 #1 `inv-28d87355496a` | 动作已恢复（prediction improved），任务却 `aborted/budget_exhausted`、结论为空 | 恢复后预算耗尽仍走 `_abort`；改为 recovered 时提交确定性总结，并为写动作预留至少 1 次模型调用 |
| F01 #2 `inv-d82bf5cb0ea4` | 审批后未执行，home 仍 500；提案保持 `approved` | 调查阶段用尽 10 次模型调用；默认改为 20，并在审批前检查反馈预留 |
| F01 #3 `inv-d47e19766e27` | 诊断正确，但回滚请求被 `duplicate` 拒绝 | 预算阻止执行的 `approved` 提案占住幂等键；新增 `executor.abandon` 转 REJECTED 并释放键 |
| F01 #4 `inv-120e286e8bcc` | `invalid_report` | 结构化报告引用/字段校验失败，保留为纠正上限证据 |
| F02 #2 `inv-15183fc1a260` | 实际已恢复，执行器判 `no_improvement` | 90 秒窗口末尾才恢复，尾 4 个样本中 1 个 500；窗口改为 135 秒 |
| F02 #4 `inv-18f3d92d1d77` | 同上，任务 `needs_human` | 尾段 4 个样本中 1 个预热失败；尾段样本改为 `max(2,min(3,samples//2))`，最后 3 个全 200 时判 improved |
| F02 #1 | 工具预算耗尽后直接终止，未提交报告 | 工具预算耗尽改为可恢复错误，让模型用剩余模型调用提交报告 |
| F03 #1 | 27 次工具、0 条关系 | 模型把评估留到最后；提示词增加“关键观测后立即评估”，并在剩余 6 次工具时提醒一次 |
| A01 | checkpoint 反序列化提示未注册 `ReportDraft` | 当前版本可用；记录为已知提示，后续可显式注册 `allowed_msgpack_modules` |
| V08 快照兼容 | `uv run aiops eval` 的 V08 报「追问后结论、证据或授权已变化」 | 假设摘要新增 revision/refutation 字段后，旧调查的 v2 快照全部失配；改为只有单 Agent 快照纳入新字段，并补 `tests/test_eval_v08.py` 回归，V08 恢复通过 |

## 3. 验收矩阵

| 计划要求 | 证据 | 结果 |
|---|---|---|
| F01/F02/F03 各至少 3 次有效新 Agent 运行，各至少 1 次正确调查 | 上表与第 1.1 节分母表：F01 8 次、F02 5 次、F03 5 次，均有正确调查记录 | 满足；F03 定位到 selector/代理插入，但没有声称代理内部机制 |
| A01 独立用例 | `a01-01-*`、`outputs/final-db-state.txt` | 满足；不并入 F01–F03 分母 |
| F01 审批回退并把恢复结果返回 Agent | `f01-06-approve.txt`、`f01-07-approve.txt`、`f01-08-wrong-lead-approve.txt` | 满足；审批是独立进程 |
| A01 自动重启与恢复归属 | `a01-01-investigate.txt`：旧 UID `9ca9b39b…` → 新 UID `46fd6423…`，模板不变，outcome improved | 满足 |
| 错误初始线索被纠正 | `f01-08-wrong-lead-*`：从 frontend 开始，最终回退 productcatalogservice | 满足 |
| 未确认根因的诊断写 | A01 诊断性 `restart_pod`，调查仍为竞争假设；F01 #1 也是 diagnostic 回滚 | 满足；报告写明证据边界 |
| 审批等待跨进程退出 | `f01-06-investigate.txt` 退出时 `waiting_approval`，随后独立 `approve` 恢复 | 满足 |
| no_improvement/worsened/unknown 契约 | F02 #2/#4 真实 `no_improvement`；`tests/test_actions_*`、`tests/test_outcome.py` 覆盖 worsened/unknown | 部分满足；worsened/unknown 只有自动化证据 |
| 重放不二次写 | `f02-05-replay-approve.txt`：第二次 approve 返回 `not_pending`，attempt 数 1→1 | 满足 |
| F03 停止且 cleanup 不算恢复 | `f03-03-investigate.txt` 无提案；`f03-03-cleanup.txt` 恢复 18ms；两个 task `recovered_at=None` | 满足 |
| 真实环境初始/中间/最终截图 | 见第 4 节 | 满足 |
| `make check-all` | 2026-11-11：**386 passed, 1 warning**；ruff check/format 通过 | 满足 |
| 既有 V01–V10 不降低 | 2026-11-11 `uv run aiops eval`：V01–V10 全部通过（含 V08 快照兼容回归） | 满足 |

## 4. 截图

| 场景 | 初始 | 关键中间 | 最终 |
|---|---|---|---|
| F01 | `screenshots/f01-initial-500.png` | `screenshots/f01-waiting-approval.png` | `screenshots/f01-recovered.png`、`screenshots/f01-final-200.png` |
| F01 错误线索 | `screenshots/f01-wrong-lead.png` | — | 同上 F01 最终 |
| F02 | `screenshots/f02-initial-500.png` | `screenshots/f02-oom-before.png` | `screenshots/f02-recovered.png`、`screenshots/f02-final-200.png` |
| F03 | `screenshots/f03-latency.png` | `screenshots/f03-stop.png` | `screenshots/f03-cleanup.png` |
| A01 | `a01-01-inject.txt` / `a01-01-verify.txt` | `screenshots/a01-recovered.png` | `a01-recovered.png` |
| 重放 | — | `screenshots/f02-replay-approve.png` | — |
| 数据库 | — | `screenshots/db-task-states.png` | — |

UI 截图由 headless Chrome 直接渲染真实前端页面；文本截图直接渲染原始输出文件，不使用桌面窗口或人工排版。初始/最终状态各重新注入并复位一次，命令记录在 `outputs/f01-09-screenshot-*.txt`、`outputs/f02-06-screenshot-*.txt`。

## 5. 复现命令

```sh
# 环境
export KUBECONFIG=$HOME/.kube/vigil-lab.yaml
kubectl -n vigil-monitoring port-forward svc/prometheus 9090:9090
kubectl -n vigil-shop port-forward svc/frontend 8080:80

# F01：注入（脚本会自行回退；截图用手动注入保留故障）
kubectl -n vigil-shop set image deployment/productcatalogservice server=vigil/productcatalogservice:canary-20261104

uv run aiops investigate --service productcatalogservice \
  --symptoms "首页大量 500，productcatalogservice 刚发布后商品目录不可用" \
  --namespace vigil-shop --lookback-minutes 15 --output json
uv run aiops approve <proposal-id> --actor operator --reason "批准诊断性回滚"
uv run aiops report <investigation-id>

# F02
faults/f02-memory-limit-oom/repeat.sh inject
faults/f02-memory-limit-oom/repeat.sh verify

# F03
faults/f03-redis-latency/repeat.sh inject
faults/f03-redis-latency/repeat.sh verify
faults/f03-redis-latency/repeat.sh cleanup

# A01
faults/a01-pod-rebuild/repeat.sh deploy
faults/a01-pod-rebuild/repeat.sh inject
faults/a01-pod-rebuild/repeat.sh verify
```

当前集群状态：F01 使用官方稳定镜像，F02 限额 128Mi/64Mi，F03 selector 已恢复 `app=redis-cart`，home 200、probe 1；A01 的 `rebuild-demo` 仍在 `vigil-experiment`，用于补充截图，验收完成后由操作者清理。

## 6. 边界

- 本轮验证的是单机实验集群与单 Agent 主循环，不是生产高可用、权限治理或任意故障覆盖。
- F03 只到“selector 指向代理、代理插入”这一层；没有代理日志/逐跳延迟，未确认代理内部实现，因此报告按证据不足停止。
- A01 的调查结论是两个竞争假设都 supported，但动作结果只能证明“重建使本地状态丢失后恢复”，不能单独证明应用初始化依赖；报告保留了这一边界。
- F02 的 `worsened`/`unknown` 仍主要靠自动化测试；真实集群目前只有 `no_improvement` 失败记录。
- 旧 `demos/ms01`–`ms08` 的评分与运行记录未被本轮改写；本轮结果不重复计入旧 V01–V10 分母。
