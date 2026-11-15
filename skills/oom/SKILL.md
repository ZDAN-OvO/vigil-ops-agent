---
name: oom
description: 排查内存相关故障：容器显示 OOMKilled、内存接近 limit 或用量持续增长时，区分上限过低、负载增长与泄漏，核对终止原因、重启次数和内存曲线。手动调查没有告警名时也适用。反复重启本身不能证明 OOM；无内存证据、模板和镜像未变的单 Pod 状态异常考虑 pod-rebuild，版本变化相关故障用 bad-release，下游超时用 dependency-latency。持续 OOM 不靠重建 Pod 解决。
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
