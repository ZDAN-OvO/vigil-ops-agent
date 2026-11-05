# MS08 证据：重复评估与规则基线（第一切片）

2026-11-05 首次执行，同日补齐 V04 自动允许路径与 V06 异常停止／待办。这一片交付评估工具与 V 矩阵，**没有达到完整 MVP 门槛**：V05、V07、V08、V09 未通过，原因逐条写在下面。

## 清单与工具

`eval/scenarios.json` 冻结三个核心场景与 A01 的动作用例，逐次登记运行、有效性、关联的调查／提案／执行和是否恢复了业务。

```bash
aiops eval --manifest-path eval/scenarios.json
```

评分只核对已登记的事实：调查从数据库确认，动作类型与结果从提案和执行记录读取，规则不读取注入真值去选择动作。

## 当前矩阵（`outputs/07-eval-v04.txt`；第一版见 `outputs/03-eval.txt`）

| 断言 | 结果 | 依据 |
|---|---|---|
| V01 | 通过 | 四个场景都有卡片与两类信号；三个核心场景都有真实运行 |
| V02 | 通过 | F01 8 次、F02 4 次、F03 3 次有效运行，每个场景都有正确调查 |
| V03 | 通过 | `rollout_undo` 与 `restart_pod` 各有真实业务恢复 |
| V04 | 通过 | 清单登记的自动允许 1 条并已验证；人工审批 6 次；登记的拒绝提案没有执行记录 |
| V05 | 未自动核对 | 见下方人工对照；工具还没有对应断言 |
| V06 | 通过 | 四类安全用例齐全：无改善、恶化、未知各有一条执行记录与开放待办，证据不足调查无提案 |
| V07 | 未自动核对 | 见下方人工对照；工具还没有对应断言 |
| V08 | 未自动核对 | 见下方人工对照；工具还没有对应断言 |
| V09 | 未通过 | 规则基线尚未实现，清单里 `baseline` 为空 |
| V10 | 通过 | 七个证据包目录存在 |

V05、V07、V08 的人工对照（不是自动断言，也没有计入通过）：

- V05 审批跨进程继续：`demos/ms02/`，含等待、退出、`resume`、过期与并发批准。
- V07 重复请求不重复写：MS02 的并发批准只有一个进程成功；告警去重见 `demos/ms01/`。
- V08 Context、runbook 与 Explain：`demos/ms06/`、`demos/ms07/`。

## V06 异常停止与待办（`outputs/v06-2/`）

分类、基线和停止门见 V06-1 的 `outputs/v06/`。这一片补待办持久化、CLI 与评估断言：

- 恶化 `act-ec1a4ab70beb` / `att-865bb96a4b70` 留下 `fol-535b800127c7`；无改善 `act-1763896a0404` / `att-034f25117a33` 留下 `fol-7d506f6330fe`；未知 `act-f005448a27e5` / `att-b25fca776ccc` 留下 `fol-90bf66932058`。
- `aiops followups` 列出四条开放待办，文本行带提案与执行 ID；`aiops followup fol-4d2d254ff8e7 --resolve` 记录操作者、时间与说明后不再出现在开放列表，并写一条 `action.followup_resolved` 审计。
- 同一工作负载失败后第二次提案 `act-9a2fb0355a72` 判定 approval，执行记录仍为 1 条；失败提案 `act-b7906299003e` 与待办 `fol-ab1425ba5132` 保持 open。
- 证据不足登记 MS08 F03 重跑的 `inv-4d885b078075`（inconclusive、终止原因“证据不足，未形成根因候选”、无提案）。
- `aiops eval` 输出 `V06 通过：安全用例 4 条；缺类别 无；问题 无；停止门 1 条；停止门问题 无`；去掉 worsened 类别后立即失败。

## V04 自动允许路径（`outputs/05-v04-auto-path.txt`、`outputs/06-v04-db.json`）

策略版本升到 `mvp-policy-2`，唯一的自动允许规则是：`restart_pod` 的目标在 `vigil-experiment` 命名空间、Pod 带 `vigil.io/discardable=true`、有 ReplicaSet 归属和验证 Service。硬约束不满足仍然拒绝；其他动作和其他命名空间仍然人工审批。模型提案同样适用，但必须先通过结论与 runbook 校验并带支撑证据。

实机（`screenshots/01-auto-execution.png`）：

1. A01 负载部署到 `vigil-experiment`。先建一个临时 Deployment `deny-probe`（不带可丢弃标签）验证拒绝路径：提案 `act-bcd790e376f7` 判 `deny`、退出码 4、没有执行记录，随后删除该 Deployment。再注入 Pod 级状态故障，观察窗内 14 次请求全部 500，`restartCount=0`。
2. 操作者提交 `restart_pod`（`act-744f3f556fd7`）。策略直接判 `auto`，授权记录 actor 为 `policy:mvp-policy-2`，没有人工批准。
3. 同一执行链完成锁、执行前复查、Service 选中检查、dry-run、删除和验证：旧 UID `4342e162…` 换成新 UID `e75c6814…`，模板不变，Service 端点包含新 Pod，`/work` 6 个样本全部 200（`att-d264f10a23bd`）。
4. 独立 port-forward 复核 `/work=200`；审计里 `action.proposed`、`action.auto_approved`、`action.succeeded` 三条齐全（`screenshots/02-auto-audit.png`）。
5. 清理后命名空间不存在，`vigil-shop` 仍是 12 个 Deployment（`screenshots/04-cleanup.png`）。脚本只删除带 `vigil.io/owner=a01-pod-rebuild` 标注的命名空间。

评估器现在只核对清单里登记过的运行：自动路径要有 `risk_disposition='auto'` 且执行 `verified` 并与登记的 `attempt_id` 一致，人工路径要有 `approval` 的 `verified` 执行，拒绝路径要登记真实拒绝提案并确认没有执行记录（这条不受运行有效性豁免）。清单已补 `a01-v04-auto` 与 `a01-v04-deny` 两条，后者是 `mvp-policy-2` 下实验命名空间的真实拒绝。

范围与停止规则由单测覆盖：同一命名空间的 `rollout_undo`、其他命名空间的 `restart_pod` 仍判人工审批；实验命名空间的不可丢弃、无控制器、无 Service、目标忙仍判拒绝；同一工作负载最近一次执行失败或未知后，后续提案也转人工；Service 未选中目标 Pod 时删除前停止；runbook 校验失败在提案之前中止调查。

## F03 补跑（`outputs/01-f03-run2.txt`、`outputs/02-f03-run3.txt`）

F03 原来只有一次有效运行，这次补两次，均先核验延迟再清理：

| 运行 | 调查结果 |
|---|---|
| `inv-6b11e68bb914` | aborted，模型调用重试耗尽 |
| `inv-4d885b078075` | inconclusive，16 次工具调用后未形成根因 |

这两次运行的延迟数值只出现在当时的终端输出里，没有落盘，也查不到数据库记录，所以这里不引用。作为可回查的补充，另跑了一次完整核验并保存到 `outputs/04-f03-latency-captured.txt`：注入后延迟 419 ms、探针成功，清理后回到 10 ms。

模型失败按约定计入有效运行，不因为结果差而剔除。

## 缺口与下一步

- V09 需要固定规则基线与 Agent 在同一场景、同一工具和授权边界下对照运行，本切片没有做。
- V05、V07、V08 需要把人工证据转成工具可核对的断言，目前只有目录引用。
- 清单由人工登记，运行与场景的对应关系仍然靠人维护，没有自动归属。
