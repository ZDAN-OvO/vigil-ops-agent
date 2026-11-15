---
name: pod-rebuild
description: 排查单个 Pod 的运行时状态异常：手动调查没有告警名时也适用，包括 Pod 反复重启或不就绪、Pod 仍 Ready 但业务持续报错、容器重启后故障仍在。若同一工作负载的其他 Pod 正常、模板和镜像未变，或单副本下怀疑 Pod 局部状态损坏，读取本 Skill 核对证据后判断能否安全重建；这些线索是待验证假设，不是重建授权。发布或版本变化相关故障用 bad-release；OOMKilled 或内存逼近上限用 oom；下游超时或依赖变慢用 dependency-latency，不用重建掩盖这些问题。
metadata:
  vigil-auto-load-sources: alert manual
  vigil-auto-load-alertnames: PodCrashLooping PodNotReady
---

# Pod 重建判断

## 适用条件

- 异常集中在单个 Pod，同 Deployment 的其他 Pod 正常。
- Pod 处于 CrashLoop、未就绪或状态损坏，但 Pod 模板与镜像没有变化。
- 依赖链路和入口现象不能解释为该 Pod 自身的问题。

## 必须核对

1. Pod 名称、UID、所属 Deployment 与当前模板版本。
2. 同 Deployment 其他 Pod 的状态，排除模板级问题。
3. 目标 Pod 日志中的退出原因。
4. 业务验证目标：用哪个 Service 和路径判断恢复。

## 动作约束

- `restart_pod` 只能用于单个 Pod 的状态损坏；依据至少包含一条指向该 Pod 的平台观测和一条业务侧观测。
- 重建后核对 Pod UID 已改变、Deployment 模板未变、业务请求恢复。
- 重建失败或结果未知时转人工，不重复执行。
