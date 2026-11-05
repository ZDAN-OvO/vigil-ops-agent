# MS05 证据

2026-11-05 首次执行。包含 A01 机制实验和 `restart_pod` 动作链两部分；调查侧还没有生成 `restart_pod` 提案的能力，提案由操作者发起。

## 截图

CLI 类证据按约定先渲染再截图。脚本 [`scripts/render_evidence.py`](../../scripts/render_evidence.py) 读取 `faults/a01-pod-rebuild/runs/` 里的真实输出，套上终端样式页面后用无头 Chrome 截图，不改写内容。每张图底部写明来源文件，图片下方注明渲染时间。

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

原始输出仍在 `faults/a01-pod-rebuild/runs/`，时间线在同目录的 `2026-11-05.md`。

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
