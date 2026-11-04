# MS02 证据报告：审批跨进程继续

状态：2026-11-04 在 `vigil-lab` 完成真实运行，审批等待、进程退出、重启查询、批准或拒绝、重新校验、单次执行与验证全部走到。原命令记录与集群观测在本目录 `outputs/`。本报告只按实际状态陈述。

## 1. 结论

- 提案与审批决定持久化在 PostgreSQL；命令进程退出后，新进程能查到同一提案，批准后继续执行并验证业务恢复。
- 审批绑定提案哈希、目标 UID／resourceVersion 和有效期；过期授权不执行，目标漂移和提案内容变化则不沿用旧授权。
- 重复批准只有一个进程写入；重复继续执行被拒绝；拒绝路径不产生任何集群写操作。

## 2. 环境与版本

| 项目 | 值 |
|---|---|
| 集群 | `vigil-lab`，k3s v1.35.5+k3s1，单节点 |
| 业务 | Online Boutique v0.9.0，12 个 Deployment |
| 实验镜像 | `vigil/productcatalogservice:canary-20261104`，中性标签与中性错误文本 |
| 策略 | `mvp-policy-1`，`rollout_undo` 仅限 `vigil-shop`，首次执行需人工审批 |
| 审批有效期 | 默认 900 秒；过期路径用 `VIGIL_ACTION_APPROVAL_TTL_SECONDS=2` |
| 验证窗口 | 滚动完成后等两个抓取周期，再看 90 秒窗口末段样本 |

## 3. 时间线

所有时间均为 UTC。

| 时间 | 阶段 | 证据 |
|---|---|---|
| 15:23:34 | 基线：健康 digest、revision 27、`probe_success=1`、首页 200、0 告警 | [`01-baseline.txt`](outputs/01-baseline.txt) |
| 15:23:42 | 注入中性坏镜像，revision 28 | [`02-inject.txt`](outputs/02-inject.txt) |
| 15:24:05 | 首页 500，`probe_success=0` | 时间线记录 |
| 15:24:28 | 提案 `act-83ecd5d53f68`，目标 revision 28 → 27，判定 approval，退出 3 | [`03-proposal.txt`](outputs/03-proposal.txt) |
| 15:24:41 | 新进程查询：待审批提案在列，提案显示“审批：无”“执行：尚未开始” | [`04-query-after-restart.txt`](outputs/04-query-after-restart.txt) |
| 15:24:57 | 批准但 `--defer` 不执行，进程退出；有效期至 15:40:00 | [`05-approve-defer.txt`](outputs/05-approve-defer.txt) |
| 15:25:13 | 新进程 `resume`，15:27:18 判定 `verified`，7 个样本末段 0 失败 | [`06-resume.txt`](outputs/06-resume.txt) |
| 15:27:32 | 重复 `resume` 返回 `already_executed`，退出 1；集群 revision 29、`probe_success=1`、首页 200 | [`07-duplicate-and-recovery.txt`](outputs/07-duplicate-and-recovery.txt) |
| 15:27:51 | 第二次注入，revision 30，首页 500 | [`08-second-inject.txt`](outputs/08-second-inject.txt) |
| 15:28:36 | 有效期 2 秒的批准过期，`resume` 返回 `expired`；revision 仍为 30 | [`09-expired.txt`](outputs/09-expired.txt) |
| 15:28:58 | 对同一目标提案 `act-fec2807deece` 执行拒绝，无执行记录 | [`10-rejected.txt`](outputs/10-rejected.txt) |
| 15:29:24 | 两个进程同时批准 `act-58394bfae960`：一个成功，一个 `not_pending`，审计只有一条 `action.approved` | [`11-concurrent-approval.txt`](outputs/11-concurrent-approval.txt) |
| 15:31:44 | `resume` 完成，`verified`；revision 31、健康 digest、`probe_success=1`、首页 200、0 告警、坏 RS 0 | [`12-final-recovery.txt`](outputs/12-final-recovery.txt) |
| 15:32:34 | 健康版本上拒绝一次危险提案（31 → 30），无写操作 | [`10b-rejected-current.txt`](outputs/10b-rejected-current.txt) |

## 4. 分步说明

1. **提案持久化。** `act` 写入目标 UID、resourceVersion、from/to revision 与幂等键，状态 `pending_approval`。命令退出后状态不变。
2. **重启查询。** 每次 CLI 调用都是独立进程；`approvals` 列出待审批项与等待时长，`proposal` 显示单条提案、审批和执行记录及审计。查询本身不改变状态。
3. **批准与继续分离。** `approve --defer` 只记录决定，状态变为 `approved`，不写集群。`resume` 在另一个进程中重新读取提案、审批和尝试记录，再校验后才执行。默认的 `approve`（不带 `--defer`）仍在同一进程内完成批准与执行，行为与 MS01 一致。
4. **执行前重新校验。** 顺序是：已有执行记录则停（`already_executed`）；审批已过期则把提案置 `expired`（`expired`）；提案哈希与批准记录不一致则置 `failed`（`hash_mismatch`）；再取目标互斥锁、重新 plan、比对 UID 与 resourceVersion（`drift`）。任何一步失败都不发写请求。
5. **单次执行。** 目标锁加 `action_attempts.proposal_id` 唯一约束保证同一提案只写一次。重复 `resume` 返回 `already_executed`，退出 1。
6. **并发批准。** 状态迁移用单条 `UPDATE ... WHERE status = 'pending_approval'`，两个进程只有一个 rowcount 为 1；另一个读到 `approved` 并返回 `not_pending`。本次实机只有一个审批记录、一条 `action.approved` 审计。
7. **过期路径。** 批准时写入 `expires_at`；超过后 `resume` 把提案置 `expired`，不需要也不允许沿用旧授权。集群没有任何写操作，revision 保持不变。
8. **拒绝路径。** 拒绝只写审批记录与状态，不创建执行记录。第一次拒绝（15:28:58）在修复前构建上运行，输出多显示了一行无效期；修复后拒绝不再设置 `expires_at`，15:32:34 在健康版本上重做并留档。
9. **验证与恢复。** `verified` 只看滚动完成后的 90 秒窗口末段样本；最终一次的窗口中段有 1 个失败样本（预热），末段 0 失败，因此判定恢复。集群侧同时核对镜像 digest、revision、首页、告警与坏 ReplicaSet 副本数。

## 5. 可查记录

- 跨进程批准执行：`act-83ecd5d53f68`，审批 `ms02-reviewer-1`，执行 `att-6fdd65ccc940`，`verified`。
- 过期：`act-79ff5b02dcd0`，有效期 2 秒，状态 `expired`，无执行记录。
- 拒绝：`act-fec2807deece`（旧构建）、`act-7455515e0767`（当前构建），均 `rejected`，无执行记录。
- 并发批准：`act-58394bfae960`，审批 `ms02-reviewer-b`，执行 `att-50919114d60b`，`verified`。
- 原始验证引用在 `runtime/evidence/action-check-vigil-lab/`，不入库。

## 6. 失败与缺口

1. 审批过期只比对本地时钟，没有撤销、提醒或重新认证审批人；操作者必须自己查询。
2. `resume` 只在“没有执行记录”时继续。若进程在写入意图后崩溃，本里程碑不自动协调，按 MS09 处理。
3. 漂移（`drift`）与哈希不一致（`hash_mismatch`）只有单元测试覆盖；本次实机没有在批准后故意改动目标。
4. 并发只验证了两个同时批准。两个同时 `resume` 依靠目标锁与唯一约束，未做实机对撞。
5. 待审批提案本身不会过期；只有批准后的授权有有效期。
6. 审批人只是字符串 actor，没有身份校验或权限系统。
7. 实机运行两次注入坏镜像，第一次用于跨进程批准路径，第二次用于过期和拒绝路径；两者都已在 `outputs/` 留档。

## 7. 复现

前置：集群健康，Prometheus 与前端的 port-forward 在位，`.env` 含数据库与 kubeconfig。

```bash
kubectl port-forward -n vigil-monitoring svc/prometheus 9090:9090 --address 127.0.0.1
kubectl port-forward -n vigil-shop svc/frontend 8080:80 --address 127.0.0.1
kubectl -n vigil-shop set image deploy/productcatalogservice server=vigil/productcatalogservice:canary-20261104
uv run --frozen aiops act --deployment productcatalogservice --reason "MS02 复现" --actor operator
uv run --frozen aiops approve <提案 ID> --actor reviewer --defer
uv run --frozen aiops proposal <提案 ID>
uv run --frozen aiops resume <提案 ID> --actor reviewer
```

过期路径在批准时加 `VIGIL_ACTION_APPROVAL_TTL_SECONDS=2`，等待数秒后 `resume`。

## 8. 复位

运行结束后 `productcatalogservice` 已回到健康 digest，revision 31，`probe_success=1`，首页 200，活动告警 0，坏 ReplicaSet 0 副本。port-forward 由操作者停止；没有执行集群删除。
