---
name: dependency-latency
description: 排查依赖链路延迟：判断延迟来自本服务、下游依赖还是入口流量，优先只读定位，不执行写操作。
metadata:
  vigil-auto-load-sources: alert
  vigil-auto-load-alertnames: RedisLatencyHigh DependencyLatencyHigh
---

# 依赖链路延迟排查

## 判断线索

- 本服务错误率正常但请求延迟上升，或 Redis/数据库调用耗时上升。
- 入口流量没有明显变化，但下游连接数、超时或重试增加。
- 延迟在多个 Pod 上同时出现，说明不是单个 Pod 的问题。

## 取证顺序

1. 用 Prometheus 对比入口请求延迟、下游调用延迟和超时率，确认拐点时间。
2. 列出 Pod 与 Service，确认副本数、就绪状态和是否存在单点。
3. 读取目标 Pod 日志，查找连接超时、重试或连接池耗尽。
4. 用依赖侧指标区分“下游变慢”和“本服务等待放大”。

## 停止

- 依赖链路问题通常不能通过重启本服务 Pod 解决。
- 证据不足以定位到可安全执行的写动作时，给出人工待办并停止；不要执行写操作。
