# MS05 证据

2026-11-05 首次执行。包含 A01 机制实验、`restart_pod` 动作链，以及调查侧生成 `restart_pod` 提案的完整链路。

## 截图

CLI 类证据按约定先渲染再截图。脚本 [`scripts/render_evidence.py`](../../scripts/render_evidence.py) 读取真实输出，套上终端样式页面后用无头 Chrome 截图，不改写内容。A01 实验图来自 `faults/a01-pod-rebuild/runs/`，动作链与调查图来自本目录的 `outputs/`。每张图底部写明来源文件，图片下方注明渲染时间。

| 文件 | 内容 |
|---|---|
| `00-deploy.png` | 部署后 Pod 就绪，`/work` 200 |
| `01-inject.png` | 基线 `/work` 200，写坏状态后 500 |
| `02-fault-sustained.png` | 60 秒观察窗 60/60 请求失败，Pod 未变化 |
| `03-container-restart.png` | 同一 Pod UID，`restartCount` 加 1，20/20 仍失败 |
| `04-rebuild.png` | 新 UID，模板哈希与所属 ReplicaSet 不变，6 秒恢复 |
| `05-platform.png` | Pod、Deployment、事件与容器终止原因 |
| `06-second-run.png` | 第二轮的注入、验证、容器重启与重建 |
| `07-cleanup-baseline.png` | 清理后 12 个工作负载，`probe_success=1`，0 告警 |
| `08-propose.png` | `restart_pod` 提案与规则判定 |
| `09-approve-verified.png` | 审批后执行，结果 `verified` |
| `10-denied-business-pod.png` | 业务 Pod 被规则拒绝 |
| `11-audit.png` | 提案、审批、执行与审计记录 |
| `12-cluster-after.png` | 新 Pod、Service 端点与事件 |
| `13-proxy-check.png` | Service 代理 503 与 Pod 代理 200 的对照 |
| `14-investigate.png` | 调查结论：Pod 级状态故障，建议重建 |
| `15-model-proposal.png` | 模型生成的提案与审批后的验证结果 |
| `16-investigation-audit.png` | 调查与动作的审计链 |

实验的原始输出在 `faults/a01-pod-rebuild/runs/`，时间线在同目录的 `2026-11-05.md`；动作链与调查的原始输出在 `outputs/`。

## restart_pod 动作链（2026-11-05）

在 A01 场景上跑通完整链路：提案 → 规则只给审批 → 人工批准 → dry-run → 带 UID 前置条件的删除 → 控制器重建 → 真实请求验证。

| 环节 | 结果 |
|---|---|
| 提案 | `act-8c4d56f1f1fa`，目标 Pod `rebuild-demo-…-mlxsm`，ReplicaSet `rebuild-demo-58958844c8` |
| 拒绝路径 | 对业务 Pod `frontend-…` 提案得到 `act-2cf158999f5e`，理由“未标记为可丢弃工作负载”，没有生成执行记录 |
| 审批 | `ms05-demo` 在 2026-11-05T04:07:18Z 批准，有效期 15 分钟 |
| 执行 | `att-cd263265f070`，删除旧 UID `8e2ad336…`，新 Pod `…-m8hfj` UID `5e0b0414…` |
| 验证 | 90 秒窗口 6 次请求全部 200，模板哈希不变，Service 端点为新 Pod，观察结束后重读仍就绪且 0 重启，结果 `verified` |
| 清理 | 实验负载删除，集群回到 12 个工作负载 |

两条实现取舍：

- 删除只用 UID 作前置条件。Pod 的 `resourceVersion` 随状态更新不断变化，绑定它会让删除在读取后几毫秒内就 409。
- 业务验证经 API 服务器代理直接请求重建后的 Pod，并单独确认 Service 的端点里包含新 Pod。k3d 里 apiserver 到 ClusterIP 不通，Service 代理返回 503，所以不用 Service 代理做业务检查。

失败保留：修复前有两次执行被记为 `unknown`。`att-502fddc3c681` 记录了 `precondition_failed`、HTTP 409；更早的 `att-2dc0d9521259` 当时还没记录错误详情，只留下 `ActionError` 和 `apply` 阶段，从时间（19 毫秒）看不可能走到业务检查。另有一次 `failed`（`att-22bf0e81c666`），6 次业务样本全部失败且模板哈希不一致，直接原因是当时用 Service 代理取业务响应，而 k3d 里该代理返回 503。三次都按“停止、不重放”处理，记录留在数据库。

| 输出 | 内容 |
|---|---|
| `outputs/01-propose.txt` | 提案与规则判定 |
| `outputs/02-approve.txt` | 审批后执行与验证结果 |
| `outputs/03-denied-business-pod.txt` | 业务 Pod 被拒绝 |
| `outputs/04-proposal-state.txt` | 提案、审批、执行记录 |
| `outputs/05-audit.txt` | 审计事件 |
| `outputs/06-cluster.txt` | 新 Pod、端点与事件 |
| `outputs/07-cleanup.txt` | 清理与基线 |
| `outputs/08-service-proxy.txt` | Service 代理与 Pod 代理的行为对照 |
| `outputs/09-investigate.txt` | 调查输出与结论 |
| `outputs/10-model-proposal.txt` | 模型提案的状态 |
| `outputs/11-model-approve.txt` | 审批后的执行与验证 |
| `outputs/12-investigation-audit.txt` | 调查与动作的审计记录 |

## 调查侧生成 restart_pod 提案（2026-11-05）

调查的 `Assessment` 增加 `restart_pod` 取值与 Pod、Service、路径三个字段。提示词只在目标 Pod 带 `vigil.io/discardable=true`、业务请求持续失败，且证据排除资源规格、配置模板和依赖链路原因时允许这一取值；没有标签一律 `none`。

运行时还有两道闸：没有根因候选不提案，只采纳关系为 supporting 的证据。反证和中性证据不能撑起动作。

实机：注入同样的 Pod 级状态故障后运行 `aiops investigate --service rebuild-demo`，调查 `inv-e6a92291724e` 完成，9 条证据、13 次工具调用、5 次模型调用，结论区分了 Pod 级状态与模板、资源规格和依赖链路问题，并标注了证据边界（Prometheus 不可用、日志查不到 /work 的内部错误）。模型生成提案 `act-344aa4694ecf`，绑定 2 条支撑证据；人工批准后 `att-d195ae5d4b4d` 为 `verified`，新 Pod UID `e2ee187f…`，模板不变，90 秒 6/6 成功。

更早一次尝试 `inv-4a794afe9851` 的提案失败，审计记录 `adapter_missing`：调查运行时构建执行器时没有传 `restart_pod` 适配器。修复后重跑，记录保留。

更早那次调查用满了预算上限（16 次工具、8 次模型）才完成；预算告警仍缺失。
