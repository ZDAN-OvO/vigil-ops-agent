# 提供值班面板的数据和五张 Grafana 大盘

## 目标

运维人员打开网页就能看到当前告警、入口探测、最近变更、异常服务、Warning 事件与节点状态。需要趋势时，通过配置好的 Grafana 跳转深入查看。

## 本期不做

- 不改业务应用埋点、不加服务网格、不把业务指标缺数显示成 0。
- 保留现有首页回滚探测的抓取任务及语义。新增入口探测使用独立任务。

## 完成标准

- 告警读取 AlertManager firing 数据，并按组返回服务层可以关联的标识。入口探测按应用、地址标签分组，给出近一小时可用率、当前状态、采样点及空洞。
- 变更综合 Kubernetes ReplicaSet 历史与已核实的 Vigil 执行记录，限最近 24 小时。Vigil 回滚可显示批准人，普通发布不推断操作者。Warning 事件按最近发生时间筛一小时。
- 异常服务按 Deployment 或 Service 归属聚合，覆盖 §8 的副本、重启、等待、上次退出和资源接近上限。CPU 或内存实际用量超过对应容器限额 80% 时给提示。无可靠数据时显示缺数。节点显示健康汇总或条件异常。
- 每块返回独立的数据及失败状态。异常服务数可可靠计算时保留其值，其他块失败不把它置为空。数据接口为已知来源和固定查询，不从浏览器透传集群查询。
- 实验清单交付独立探测、Grafana 只读数据源及五张大盘。应用配置 Grafana 地址后才生成五个跳转。交付当前值，容器历史趋势按 [总纲的待定项](PLAN.md#关键取舍) 决定是否启用。未启用时显示缺数，不展示空趋势为正常。
- Grafana 发布大盘通过 Prometheus 读发布信号。监控任务负责从保留的 ReplicaSet 历史和已核实动作记录导出可关联的服务、版本、时间与变更类型，以已知样例验证普通发布与 Vigil 回滚的查询结果。导出限制在最近 24 小时和当前保留的历史，身份、凭据和模板不进指标标签。批准人只在右栏按执行记录显示。
- 本地服务交付监听地址、Bearer、`groupKey` 与 firing、resolved 样例后，由本任务更新 `monitoring.yaml`。交给集成任务验证实际通知。切换接收方和 AlertManager 配置使用同一次实验环境变更。

## 关键取舍

- 直接查询现有 Prometheus、AlertManager 和 Kubernetes。右栏给即时判断，历史趋势给 Grafana。缓存若有需要只限制面板重复读取，不参与 Agent 取证。
- 归属、聚合时间、缺数与查询失败形成后端快照，由前端统一展示。告警对应会话的关联由本地服务按 `groupKey` 计算。

## 前置依赖

- 无。使用代表性数据开发。告警会话关联、监听和实际凭证配置在 [本地服务与告警](TODO_serve-alerts.md) 就绪后接入。

## 已核实事实

- `env/manifests/monitoring.yaml:27` 已抓取 kube-state-metrics，`:36` 的 `frontend-probe` 是现有首页探测，`:72` 配置告警分组。
- `src/aiops/actions/executor.py:193` 依赖 `frontend-probe` 验证回滚。
- `src/aiops/tools/prometheus.py:106` 有区间查询。`src/aiops/tools/kubernetes.py:112` 有 ReplicaSet 裁剪，`:299`、`:315`、`:419` 分别读取 Pod、Deployment、事件。
- `tests/test_kubernetes_trim.py:93` 及 `:242` 覆盖事件时间与 ReplicaSet 数据裁剪。

## 变更范围

- 新增 `src/aiops/monitoring/` 的数据采集与聚合，复用 Kubernetes 和 Prometheus 客户端。缺少的 metrics、历史或 AlertManager 查询按面板需求补齐。共用客户端不会改变现有工具输出。
- 修改 `env/manifests/monitoring.yaml` 并在 `env/manifests/` 新增 Grafana 与大盘资源，更新 `env/README.md` 的实验部署说明。独立验证老首页探测的告警和写后验证。

## 风险与回滚

- 指标缺失、有限历史和 Pod 归属错误会让值班人员误判。用单块缺数和带身份的归属测试覆盖。
- 关闭新增的探测、Grafana 或趋势配置后，既有首页监控和验证仍可运行。切回旧 webhook 需同时回退其 AlertManager 认证和通知配置。

## 验证

- `uv run --frozen pytest -q tests/test_prometheus.py tests/test_kubernetes_trim.py`，新增监控数据归属、空采样与失败隔离用例。
- 在隔离 k3d 检查目标抓取、首页探针仍可用、五张大盘数据源、部分缺数、资源提示。发布大盘局部验收使用固定的 ReplicaSet 和已核实回滚记录样例。用 [集成与切换验收](TODO_release-verification.md) 核对真实发布和回滚记录以及真 AlertManager 的 resolved 投递。趋势测试随已确认的采集决定执行。

## 停止条件

- 现有首页探针或业务验证变更时，暂停监控清单部署。
- 若额外采集需要超出实验环境现有权限的配置，先确认趋势决定和权限范围。

## 进度

- [ ] 六块只读数据
- [ ] 探测与 Grafana
- [ ] 独立缺数与实验环境验证

## 范围外的问题

业务 SLO 和请求级指标另立需求。
