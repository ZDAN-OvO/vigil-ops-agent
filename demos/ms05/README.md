# MS05 证据

2026-11-05 首次执行。当前只有 A01 机制实验的证据，`restart_pod` 的提案、审批、执行和验证尚未实现。

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

原始输出仍在 `faults/a01-pod-rebuild/runs/`，时间线在同目录的 `2026-11-05.md`。
