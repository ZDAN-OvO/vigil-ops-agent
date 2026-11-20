# 值班数据接口

服务启动时用 `Monitoring.from_settings(settings)`（`aiops.monitoring.snapshot`）固定集群、命名空间和来源。请求时调用 `await monitoring.snapshot()`。网页只读服务端投影，客户端参数不参与 Kubernetes 查询范围、PromQL 或来源地址的选择。网页响应在关联告警会话后应用共享出站脱敏，尤其是 Warning 的说明和回滚的批准人。

快照顶层有 `collected_at`（UTC ISO 8601）、`abnormal_service_count`（整数或 `null`）、`grafana_links`（块名到 URL 的对象，未配置则为空对象），以及下列六块。每块统一返回 `{status, data, missing, failed}`。后两者分别为缺数名和失败来源名的列表。不向网页返回上游异常或原始响应。

| 状态 | 含义 |
|---|---|
| `ok` | 取得有效数据。空列表表示已查询且没有符合条件的行。 |
| `partial` | 可使用 `data` 中已取到的数据。检查 `missing` 和 `failed`，显示缺失和失败部分。 |
| `missing` | 数据源未提供形成该块所需的观测，`data` 为 `null`。 |
| `error` | 关键查询或投影失败，`data` 为 `null`。 |

| 块名 | `data` 字段 |
|---|---|
| `alerts` | `groups` 含 `name`、`group_key`、`receiver`、`alerts`、`session`。组内告警含 `fingerprint`、`severity`、`starts_at`、`duration_seconds`、`suppressed`。已恢复告警不列出。 |
| `probes` | `step_seconds`、`applications`。每组有 `application` 与 `targets`。目标有 `address`、`state`（`up` / `down` / `missing`）、`http_status`、`duration_seconds`、`availability`、`sample_count`、`points`。 |
| `changes` | `items` 是最近 24 小时各变更，含 `namespace`、`service`、`version`、`occurred_at`、`kind`（`release` / `vigil_rollback`），核实到批准人的回滚另有 `approved_by`。 |
| `services` | `abnormal_count`、`abnormal`、`healthy`、`unknown`。每行有 `name`、`kind`（`Deployment` / `Service`），异常与未知行另有 `issues`、`last_exits`、`resource_status`、`missing`。 |
| `warnings` | `items` 含 `reason`、`object`（`kind`、`name`）、`message`、`count`、`last_seen`。根据最近观测时间选最近一小时，包含重复事件的最后发生时间。 |
| `nodes` | `healthy` 是正常节点名称列表，`abnormal` 含节点 `name` 和异常的 `conditions`，`unknown` 含节点名和 `missing_conditions`。已知异常与条件缺数可同时存在。 |

## 采样和归属

- 探测点是按时间升序的 `[Unix 秒, 0 | 1 | null]`，每 15 秒一格，共 240 格。抓取失败、未采样、探测值不可用的格留空。可用率为有数据格中的成功比例（0–1）；完全无数据时为 `null`。当前状态取最近一格，HTTP 未返回状态码时为 `null`。
- Pod 按控制者 UID 经 ReplicaSet 归属 Deployment。其余 Pod 只在恰好一个 Service selector 命中时归到该 Service；重复选择或控制者无法核实会标记缺数。Service 已由 Deployment 表示时只统计一次。资源阈值按正在运行的容器各自的限额评估，严格超过 80% 产生 `resource_near_limit`。提示含 `pod`、`container`、`resource`、`usage`、`limit`、`unit`（`cores` 或 `bytes`）和 `ratio`。用量需在两分钟内且采样窗起点晚于当前 Pod 创建时间。无适用限额时 `resource_status=not_applicable`，其余状态为 `ok`、`partial` 或 `missing`。
- 异常类型另有 `replicas_not_ready`、`no_ready_pods`、`pod_pending`、`container_waiting`、`container_terminated`、`recent_container_exit`、`frequent_restarts`。近一小时至少三次重启判为频繁。Pod 在窗口内新建时用其重启计数，否则用同 UID 的 kube-state-metrics `increase` 估算，相应行标记 `estimated`。上次退出字段保留原因、退出码及可用的发生时间。
- 异常服务数取已知且可确定的服务集合。缺数若可能新增异常服务，返回 `null`；已知异常行仍保留。如果缺数只涉及已确定异常的服务，异常总数仍为确定值。其它五块的失败独立返回。

## 告警会话接点

面板查 `GET /api/v2/alerts/groups`。对于实验清单按 `alertname` 分组的路由，`group_key` 与 webhook 的 `groupKey` 同形。例如首页告警是 `{}/{alertname="FrontendProbeFailed"}:{alertname="FrontendProbeFailed"}`。默认路由使用 `{}:{alertname="…"}`。遇到其它分组标签或接收方时，保留告警和 `group_key: null`，由该块报告缺数。

服务层根据非空 `group_key` 查询未关闭告警会话，将 `session: null` 填为 `{session_id, pending_approval}`；无匹配时保留空值。切换 AlertManager 前核对新 webhook 地址、Bearer 凭据引用、`send_resolved: true` 和 firing / resolved 样例。若修改 `group_by`、匹配条件或接收方名称，同时更新 `alert_group_key` 与样例验证。接收方变更与 webhook 同次部署。

## 发布信号

普通发布来自当前 Deployment 控制的 ReplicaSet，时间为创建时间，版本为保留的最初 Deployment revision（如 `r2`）。Vigil 回滚来自 PostgreSQL 中已核实的动作，按 Deployment UID、目标版本和保留的 ReplicaSet 核对，时间取已核实尝试的开始时间。复用原 ReplicaSet 的回滚可与该 ReplicaSet 的原发布并存。Kubernetes 已裁剪历史时省略无法确认仍保留的条目。动作查询使用现行动作记录、提案和审批表；样例可以注入 `RollbackHistory`。

实验发布出口 `GET /metrics` 与右栏共用投影。指标 `vigil_release_timestamp_seconds` 的值是变更的 Unix 秒，标签只有 `namespace`、`service`、`version`、`change_type`、`change_at`（用于区分同版本的多次变更）。`vigil_release_source_available{source="replicasets|rollbacks"}` 各报 1 或 0。Grafana 的发布表对当前保留信号做即时查询。部署与查询核对见 [实验环境](../env/README.md#10-值班数据与-grafana)。
