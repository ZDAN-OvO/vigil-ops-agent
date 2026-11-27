# Online Boutique 首页依赖的资源故障

目标为 `vigil-shop/currencyservice` Deployment 中的 `server`。首页请求需要完成价格换算。 `probe_success{job="frontend-probe"}` 与 `probe_http_status_code{job="frontend-probe"}` 记录真实请求结果。

## 内存限额

操作者写入 limit `16Mi` 和 request `8Mi`，再缩到 0 并恢复为 1 个副本，使故障模板成为唯一运行版本。核对现行 Deployment revision 的容器重启至少三次、最近一次终止原因为 `OOMKilled`、首页探针失败，以及 `FrontendProbeFailed` 已触发。

自动动作把内存 limit 调到 `128Mi`。核对 Deployment 更新生效、首页恢复和新的 OOM 证据消失。保留完整的 10 分钟观察。操作者取证后用 `restore` 把内存 request 复位至 `64Mi`。

```bash
bash faults/f02-memory-limit-oom/repeat.sh inject
bash faults/f02-memory-limit-oom/repeat.sh verify
bash faults/f02-memory-limit-oom/repeat.sh status
bash faults/f02-memory-limit-oom/repeat.sh restore
```

`inject-insufficient` 同样施加低内存限额，同时用轻量 pause 容器保持进程存活，使服务缺少 gRPC 后端。 `verify-insufficient` 核对首页失败、现行 Pod 无 OOM。该变体通过带新标识的 Webhook 通知走真实入口，验证缺少 OOM 证据时进入人工处理。

## CPU 限流

`cpu-load` 创建独立负载 Deployment，默认每秒发送 1 个真实首页请求，最多 80 个在途请求。先确认 `200m` 下业务正常、限流周期占比稳定低于 0.5。宿主性能不同，可调整夹具的 `VIGIL_LOAD_RPS`，同一场景从基线到验证结束保持固定。 `cpu-inject` 将 limit 和 request 改成 `5m`，再缩为 0 并恢复为 1 个副本，使故障模板成为唯一运行版本。过去 1 分钟限流周期占比达到 0.5 且首页持续失败 1 分钟时触发 `VigilCpuLimitDegraded`。

注入时，操作者暂时提高存活检查的连续失败阈值，避免每次采集前都被平台重启容器。就绪检查继续按原规则执行。验收后 `restore` 复位存活检查。

```bash
bash faults/f02-memory-limit-oom/repeat.sh cpu-load
bash faults/f02-memory-limit-oom/repeat.sh cpu-inject
bash faults/f02-memory-limit-oom/repeat.sh cpu-verify
kubectl -n vigil-experiment logs deployment/vigil-currency-load --since=2m
bash faults/f02-memory-limit-oom/repeat.sh cpu-stop
bash faults/f02-memory-limit-oom/repeat.sh restore
```

Vigil 将 limit 改回 `200m`。保持固定负载至观察和取证结束。用 [真实集群验收](../../env/README.md#11-受控自动修复验收) 中的 CPU 隔离步骤暂缓首页 OOM 告警，并核对限流计数与分母。若低限额未能同时复现实际首页失败和限流，保留此次试验记录。

`relapse` 临时更改 currency Service 的 selector，使首页再次失败。完成取证后使用 `end-relapse` 恢复流量。
