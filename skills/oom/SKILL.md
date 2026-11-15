---
name: oom
description: 排查持续 OOM：区分内存上限过低、实际用量增长和泄漏，核对重启次数、容器状态与内存曲线后再决定是否重建 Pod。
metadata:
  vigil-auto-load-sources: alert
  vigil-auto-load-alertnames: PodOOMKilled ContainerOOMKilled
---

# 持续 OOM 排查

## 判断线索

- 容器状态显示 `OOMKilled`，或重启次数持续增加。
- 内存使用接近 limit，且在工作负载稳定时仍持续上升。
- 同一 Deployment 的多个 Pod 重复出现，说明不是单个 Pod 的偶发问题。

## 取证顺序

1. 列出 Pod，记录重启次数、容器状态和最近一次终止原因。
2. 读取目标 Pod 日志，确认是否有内存分配失败或启动后很快退出。
3. 用 Prometheus 查看容器内存使用与 limit 的关系，覆盖至少一个完整重启周期。
4. 只有单个 Pod 状态损坏且模板不变时，才考虑 `restart_pod`；持续 OOM 通常需要调整资源或修复泄漏。

## 约束

- 重建 Pod 不能解决模板级的内存上限问题；不要用重启掩盖持续 OOM。
- 业务恢复后停止写操作。
