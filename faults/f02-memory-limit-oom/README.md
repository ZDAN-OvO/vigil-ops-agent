# F02 持续内存限额不足导致 OOM

状态：2026-11-04 在 `vigil-lab` 完成两轮真实实验。第二轮由告警触发调查，最终由操作者重跑调查并批准回退；两轮都核验到持续 OOM、业务受损、告警和 Agent 回退后的恢复。运行证据见 [`demos/ms03/`](../../demos/ms03/README.md)。仓库内只有实验驱动脚本 [`repeat.sh`](repeat.sh)，不是最终统一的 `inject/verify/cleanup` 接口。

## 1. 场景与真值

场景卡见 [MVP F02](../../docs/mvp.md#f02持续内存限额不足导致-oomms03)。

- 载体：`currencyservice`，首页价格换算依赖它。
- 注入：把 Deployment 模板的内存 limit 从 `128Mi` 降到 `16Mi`，request 从 `64Mi` 降到 `8Mi`；然后把副本缩到 0 再拉起，让新规格成为唯一运行的模板。
- 预期：Pod 在启动阶段被 OOMKilled，restartCount 持续上升并进入 CrashLoopBackOff；首页返回 500，`probe_success=0`，`FrontendProbeFailed` 进入 firing。
- 反向边界：旧规格 Pod 不能继续服务；Kubernetes 自动重启容器不算恢复；只有回退到历史模板并由真实请求确认后才算恢复。
- 选择理由：`recommendationservice` 停掉时首页仍为 200（前端降级并记录 warning），不能作为业务受损证据；`currencyservice` 停掉时首页稳定 500，且 Deployment 只有一个容器、历史 revision 简单。

真值只保存在本目录和运行证据中，包括目标、限额和注入步骤。调查模型只看到集群里可观测的对象、事件和指标；本目录内容不进模型上下文。

已披露的前置异常：原规格 `128Mi` 的 `currencyservice` Pod 在实验前曾出现一次 `OOMKilled`（restartCount 1）。正式基线的 Pod 在注入前 9 分钟内为 0 重启，但“该服务在 128Mi 下完全没 OOM 过”不成立。第二轮期间 `paymentservice` 也出现一次 OOMKilled（limit 仍为 128Mi、无规格变化），属于与本次注入无关的次要异常。

## 2. 注入机制

改资源规格会触发滚动更新。单副本且新 Pod 未就绪时，滚动更新会保留旧规格 Pod，故障被掩盖。因此注入顺序固定为：

1. 校验当前 limit 为健康值、探针为 1、无活动告警。
2. patch 内存限制。
3. `scale --replicas=0`，等待旧 Pod 删除。
4. `scale --replicas=1`，新 Pod 只能使用低限额模板。

缩到 0 的操作是注入器让故障规格生效的手段，不是 Agent 动作；它本身会造成短暂无副本，但持续故障来自新 Pod 的 OOM 循环。

## 3. 冻结参数

| 项目 | 取值 | 依据 |
|---|---|---|
| 目标 | `vigil-shop/currencyservice` | 首页强依赖，失败可见 |
| 健康规格 | limit `128Mi`、request `64Mi` | 部署清单原值 |
| 故障规格 | limit `16Mi`、request `8Mi` | 低于启动占用，稳定复现 OOMKilled |
| 持续判定 | 至少 3 次重启且 `lastState.terminated.reason=OOMKilled` | 排除单次偶发 |
| 业务受损 | 首页 500 且 `probe_success=0` | 与 F01 相同的黑盒探针 |
| 告警 | `FrontendProbeFailed`，`for: 1m` | 现有规则，入口未改 |
| 恢复判定 | limit 回到 `128Mi`、就绪 Pod 0 重启、探针 1、首页 200、活动告警 0 | 集群规格与真实请求同时满足 |
| 观察窗 | 300 秒（`VIGIL_ACTION_VERIFY_SECONDS=300`） | 前端 gRPC 连接在长时间故障后按退避重连，默认 90 秒窗口不够 |

`16Mi` 和 `8Mi` 只用于本实验，不是通用阈值。恢复窗口 300 秒是本次场景的操作设置，执行器默认值未改。

## 4. 操作步骤

```bash
bash faults/f02-memory-limit-oom/repeat.sh inject    # 注入并打印 revision
bash faults/f02-memory-limit-oom/repeat.sh verify    # 最多 300 秒，核验 OOM、业务、告警
bash faults/f02-memory-limit-oom/repeat.sh restore   # 注入器恢复规格并验证基线
```

脚本依赖宿主机 `kubectl`、`curl`、`python3`，默认端口 8080 用于前端 port-forward。`restore` 是注入器的复位动作，单独记录 actor；若 Agent 已回退，它只做 no-op 确认，不计 Agent 成功。

Agent 链路由告警或手工调查触发，使用：

```bash
uv run --frozen aiops investigate --service frontend --symptoms "..." --context "alertname=FrontendProbeFailed"
uv run --frozen aiops approve <提案 ID> --actor <审批人>
```

## 5. 运行结果

两轮有效运行，时间均为 UTC。

| 阶段 | 第一轮 | 第二轮 |
|---|---|---|
| 注入 | 17:01:20，revision 4 | 17:18:13，revision 6 |
| 首次采样 OOMKilled | 17:01:41，restarts=2 | 17:18:28，restarts=1 |
| 告警 firing | 17:02:44 | 17:19:51 |
| 调查 | 手工，`inv-ad00d3655493` completed | 告警自动 `inv-aad3b4e73654` inconclusive；操作者重跑 `inv-e545a5e2facc` completed |
| 提案 | `act-a5175e380402`，16Mi → 128Mi | `act-150f872a4097`，16Mi → 128Mi |
| 审批与执行 | 17:10:45 批准，17:16:19 verified | 17:25:28 批准，17:31:10 verified |
| 注入器复位 | 17:16:31，revision 5，no-op | 17:31:20，revision 7，no-op |

两轮都在注入后约 1 分钟内出现 `OOMKilled/137` 与 CrashLoopBackOff，告警在约 90 秒内 firing。Agent 回退后 `memory_limit_restored=true`、就绪 Pod 重启为 0、探针窗口末段 0 失败。

## 6. 未完成

- 没有统一 `inject/verify/cleanup` 接口和场景参数文件；`repeat.sh` 中的目标、限额和窗口仍为固定值。
- 没有把真值、脚本和命名从调查模型可读范围中隔离；当前只保证本目录不进模型上下文。
- 告警规则仍带 `scenario: f01-bad-release` 标签。webhook 白名单会丢弃该标签，但它对 F02 语义不正确，后续应移除场景标签。
- Prometheus 未采集 cAdvisor 容器工作集；平台证据是 KSM 的限额、重启和终止原因，没有容器实际内存曲线。
- 第二轮告警自动调查因入口服务聚焦而 inconclusive，最终调查由操作者重跑。改进后的入口依赖规则和 12 次工具预算尚未在常驻入口上重复验证（MS12）。
- 只在 1 个副本、1 个注入点验证；未覆盖多副本、HPA 或外部修改。
