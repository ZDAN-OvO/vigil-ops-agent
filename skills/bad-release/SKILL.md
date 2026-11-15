---
name: bad-release
description: 排查发布回归：错误率或延迟在发布后上升、滚动更新停滞时，核对版本、副本与就绪状态，判断是否需要回滚。
metadata:
  vigil-auto-load-sources: alert
  vigil-auto-load-alertnames: HighErrorRate DeploymentRolloutStalled
---

# 发布回归排查

## 判断线索

- 现象开始时间与 Deployment 的 `observedGeneration`、镜像版本或 ReplicaSet 变化时间接近。
- 新版本 Pod 就绪失败、重启增多或错误率只出现在新版本。
- 旧版本仍健康，回滚目标明确。

## 取证顺序

1. 列出 Deployment 与 Pod，记录当前镜像、可用副本、就绪状态和创建时间。
2. 读取目标 Deployment 与 Pod 日志，确认错误是否在新版本出现。
3. 用 Prometheus 对比发布前后的错误率或延迟，时间窗要覆盖发布点。
4. 只有在证据指向新版本时才提出 `rollout_undo`；回滚目标必须是本服务的 Deployment。

## 约束

- 回滚依据至少包含一条 Kubernetes 平台观测和一条业务侧观测。
- 发布回归不等于资源不足；没有版本证据时不要回滚。
