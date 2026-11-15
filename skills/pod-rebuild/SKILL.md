---
name: pod-rebuild
description: 判断 Pod 级状态损坏并安全重建：核对 Pod 自身异常、模板未变和业务验证目标，再请求 restart_pod。
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
