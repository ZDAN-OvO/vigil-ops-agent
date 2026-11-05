# MS03 证据报告：持续 OOM 调查与处置判断

状态：2026-11-04 在 `vigil-lab` 完成两轮真实运行。第一轮打通注入、告警、调查、回退和验证；第二轮补齐“重建 Pod 不适用”的明确判断，并留下告警自动调查失败后由操作者重跑的记录。命令输出、集群观测和原始记录在 [`outputs/`](outputs/)，第二轮在 [`outputs/run2/`](outputs/run2/)。

## 1. 结论

- 降低 `currencyservice` 的内存 limit 后，Pod 反复 `OOMKilled`（exit 137）并进入 CrashLoopBackOff，首页 500、`probe_success=0`、`FrontendProbeFailed` firing；Kubernetes 自动重启没有恢复业务。
- 调查区分了入口与被调服务：`frontend` 自身 0 重启、无发布变更；故障规格在 `currencyservice` 的 Deployment，内存 limit 从历史 revision 的 `128Mi` 降到 `16Mi`。
- 调查明确说明重建 Pod 会沿用当前模板、无法修复规格不足；提案选择回退历史模板。审批后恢复 `128Mi`，窗口内 0 重启、探针末段 0 失败、首页 200、告警清零。
- 告警自动调查在本轮没有独立完成结论：第一轮因 Prometheus 端口未就绪，第二轮因只聚焦入口服务；补上入口依赖规则和工具预算后由操作者重跑成功。这是当前缺口，不是 Agent 恢复的证据。

## 2. 环境与版本

| 项目 | 值 |
|---|---|
| 集群 | `vigil-lab`，k3s v1.35.5+k3s1，单节点 |
| 业务 | Online Boutique v0.9.0，12 个 Deployment |
| 目标 | `vigil-shop/currencyservice`，单容器 |
| 健康规格 | memory limit `128Mi`、request `64Mi` |
| 故障规格 | memory limit `16Mi`、request `8Mi` |
| 告警 | `FrontendProbeFailed`，`probe_success == 0`，`for: 1m` |
| 验证窗口 | 300 秒；执行器默认 90 秒 |

故障规模为实验值，不是通用阈值。默认窗口不足以覆盖前端 gRPC 在长时间故障后的重连退避，因此本轮用 `VIGIL_ACTION_VERIFY_SECONDS=300`。

## 3. 时间线

第二轮为最终验收运行，时间均为 UTC。

| 时间 | 阶段 | 证据 |
|---|---|---|
| 17:18:13 | 基线：revision 5、limit 128Mi、0 重启、探针 1、首页 200、0 告警 | [`run2/01-inject.txt`](outputs/run2/01-inject.txt) |
| 17:18:20 | 注入：revision 6、limit 16Mi、request 8Mi | 同上 |
| 17:18:28 | 首次采样：OOMKilled/137、restarts=1、CrashLoopBackOff、首页 500 | [`run2/02-verify.txt`](outputs/run2/02-verify.txt) |
| 17:18:38 | restarts=2、`probe_success=0` | 同上 |
| 17:19:51 | restarts=4、告警 firing；webhook 收到告警，自动调查 `inv-aad3b4e73654` 启动 | 同上、[`serve.log`](outputs/serve.log) |
| 约 17:22 | 自动调查 inconclusive：8 次工具调用，假设停在“依赖链路” | [`run2/03a-auto-investigation.txt`](outputs/run2/03a-auto-investigation.txt) |
| 17:24:58 | 操作者用同一告警上下文重跑：`inv-e545a5e2facc` completed，提案 `act-150f872a4097` | [`run2/03-investigation.txt`](outputs/run2/03-investigation.txt)、[`run2/04-proposal.txt`](outputs/run2/04-proposal.txt) |
| 17:25:28 | `ms03-reviewer` 批准回退 16Mi → 128Mi，revision 6 → 5 | [`run2/07-final.txt`](outputs/run2/07-final.txt) |
| 17:31:10 | 执行 `att-d2e9f93ac862` verified：revision 7、limit 128Mi、0 重启、21 个样本 0 失败 | [`run2/05-approve.txt`](outputs/run2/05-approve.txt) |
| 17:31:20 | 注入器复位为 no-op：首页 200、探针 1、告警 0 | [`run2/06-cleanup-check.txt`](outputs/run2/06-cleanup-check.txt) |
| 17:31:21 | 最终状态 | [`run2/07-final.txt`](outputs/run2/07-final.txt) |

第一轮（17:01:20 注入，revision 4）走了同样的故障和回退路径，证据在 `outputs/01`–`10`。第一轮的执行验证同样为 `verified`，但调查结论没有明确写出“重建 Pod 无效”；第二轮补了提示规则后重跑。

## 4. 分步说明

1. **两类互补信号。** 平台侧是 `lastState.terminated.reason=OOMKilled`、exit 137、restartCount 持续上升、CrashLoopBackOff、Deployment limit 16Mi、KSM 的 `last_terminated_reason` 与 `restarts_total`；业务侧是独立黑盒 HTTP 探针的 `probe_success=0` 和首页 500。两者都与注入时间对齐，Kubernetes 事件里没有同名 OOM Event，符合 F02 不强制要求事件。
2. **调查定位。** 最终调查先排除入口：`frontend` Pod ready、0 重启、revision 1 无变更；再列出全部 Pod/Deployment，发现 `currencyservice` CrashLoopBackOff、restartCount 5、OOMKilled/137，当前 revision 6 的 limit 16Mi、request 8Mi，历史 revision 5 同镜像为 128Mi/64Mi。证据边界写明：没有应用日志或链路追踪，500 与依赖不可用是时序关联，不是直接错误码归因。
3. **重建不适用判断。** 结论原文说明：“重建 Pod 会沿用当前 Deployment 模板（currencyservice revision 6，16Mi），无法修复该故障，因此不建议重建 Pod，也不建议直接改限额或重启工作负载；可回退的历史模板（revision 5，128Mi）存在，应通过回退发布恢复规格。”
4. **提案与授权。** 提案 `rollout_undo`，参数绑定 `from_revision=6`、`to_revision=5`、`from_memory_limit=16Mi`、`to_memory_limit=128Mi`、目标 UID 与 resourceVersion；模型只能提案，审批由 `ms03-reviewer` 记录，执行前仍做哈希、目标版本和互斥锁校验。
5. **执行与验证。** 回退写回 revision 5 的模板，实际生成 revision 7；验证同时检查探针窗口末段 0 失败、Deployment limit 回到 128Mi、就绪 Pod 0 重启。窗口内 21 个样本、`failed_samples=0`、`tail_failures=0`、`memory_limit_restored=true`。
6. **Kubernetes 自动重启不算恢复。** 故障期间容器被 kubelet 重启 4 次以上，业务始终 500；恢复发生在 Agent 回退之后。报告不把任何一次自动重启计为恢复。
7. **复位。** 注入器 `restore` 在 Agent 回退后执行，revision 保持 7，只确认首页 200、探针 1、告警 0；它没有改变规格，不计 Agent 成功。

## 5. 可查记录

- 第二轮自动调查：`inv-aad3b4e73654`，inconclusive，8 次工具调用。
- 第二轮完成调查：`inv-e545a5e2facc`，7 条证据、7 次工具调用。
- 第二轮提案：`act-150f872a4097`，审批 `ms03-reviewer`，执行 `att-d2e9f93ac862`，`verified`。
- 第一轮自动调查：`inv-53b156c56eb4`，inconclusive（Prometheus 查询 unavailable）。
- 第一轮完成调查：`inv-ad00d3655493`，提案 `act-a5175e380402`，执行 `att-7077200837be`，`verified`。
- 原始验证引用在 `runtime/evidence/action-check-vigil-lab/`，不入库。

## 6. 失败与缺口

1. 自动告警调查没有独立完成本次结论。第一轮原因是宿主机 Prometheus 端口未启动，工具如实返回 unavailable；第二轮原因是模型只检查入口服务，8 次预算用尽仍停在“依赖链路”。之后补充了选择器说明、入口依赖规则、工具预算 12 和观测上限 8000 字符，由操作者重跑成功；自动路径尚未用新提示再次验证。
2. 实验前的原规格 Pod 曾有 1 次 OOMKilled；基线只证明注入前 9 分钟 0 重启，不能声称该服务在 128Mi 下从未 OOM。
3. 第二轮期间 `paymentservice` 出现一次 OOMKilled（limit 未变），调查把它标为次要异常；它不在本次注入范围内。
4. revision 历史包含探索阶段的临时规格，且 revision 6 复用了探索时创建的 ReplicaSet，创建时间显示为 16:49:34；判断依据是当前模板和相邻 revision 的规格，不是时间戳。
5. Prometheus 未采集 cAdvisor，缺少容器实际内存曲线；平台证据只有 KSM 的限额、重启和终止原因。
6. 告警规则仍带 `scenario: f01-bad-release` 标签，webhook 白名单会丢弃，但语义对 F02 不正确。
7. 本次运行时 `restart_pod` 尚未实现（MS05 后来补齐）。所以“重建不适用”是调查结论，不是被拒绝的动作尝试；即使动作可用，持续低内存限额下重建也不适用。
8. 审批人仍是字符串 actor，没有身份系统；沿用 MS02 的边界。

## 7. 复现

前置：集群健康，前端与 Prometheus 的 port-forward 在位，`.env` 含数据库、模型和 kubeconfig。

```bash
bash faults/f02-memory-limit-oom/repeat.sh inject
bash faults/f02-memory-limit-oom/repeat.sh verify
uv run --frozen aiops investigate --service frontend --symptoms "blackbox probe_success=0；首页返回 500；FrontendProbeFailed 处于 firing" --context "alertname=FrontendProbeFailed"
uv run --frozen aiops proposal <提案 ID>
VIGIL_ACTION_VERIFY_SECONDS=300 uv run --frozen aiops approve <提案 ID> --actor <审批人>
bash faults/f02-memory-limit-oom/repeat.sh restore
```

告警自动入口需要 `uv run --frozen aiops serve --host 172.20.0.1 --port 8081`，并保证 `VIGIL_PROMETHEUS_URL` 可达。

## 8. 复位

最终 `currencyservice` 为 revision 7、limit 128Mi、request 64Mi、1 个就绪 Pod、0 重启；`probe_success=1`，首页 200，活动告警 0。port-forward 由操作者停止；没有执行集群删除。旧的低限额 ReplicaSet 保留为证据，副本为 0。
