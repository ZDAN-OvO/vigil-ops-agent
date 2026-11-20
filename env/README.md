# 本地实验环境

单节点 k3d 实验环境的版本、清单、搭建与删除步骤。它是本机实验环境，不是生产环境，也不代表其他机器的容量。

## 1. 实测版本

| 项目 | 实测值 |
|---|---|
| 宿主 | Linux Mint 22.3 (Zena)，12 核，约 15.4 GiB 内存 |
| Docker | 29.9.3 |
| k3d | v5.9.0 |
| kubectl | v1.37.2 |
| Kubernetes | k3s v1.35.5-k3s1，containerd 2.2.3-k3s1 |
| 集群 | `vigil-lab`，1 个 server、0 个 agent，无 k3d LoadBalancer |
| API | `127.0.0.1:6443`，只绑定回环地址 |
| kubeconfig | `~/.kube/vigil-lab.yaml`，未写入默认 `~/.kube/config` |
| 存储 | k3s local-path，Prometheus PVC 2Gi、ReadWriteOnce |
| 命名空间 | `vigil-shop` 承载业务，`vigil-monitoring` 承载观测 |
| Python | 3.12.14（pyenv） |

k3d 与 kubectl 安装在 `~/.local/bin`，安装包均校验 SHA-256。

## 2. 固化清单

| 文件 | 内容 |
|---|---|
| [`manifests/online-boutique-pinned.yaml`](manifests/online-boutique-pinned.yaml) | 上游 Online Boutique v0.9.0 的全量 12 个 Deployment 与 12 个 Service；只把镜像从 tag 替换为 digest |
| [`manifests/monitoring.yaml`](manifests/monitoring.yaml) | Prometheus、AlertManager、blackbox-exporter、首页规则、入口探测、kubelet 容器指标和发布抓取 |
| [`manifests/kube-state-metrics.yaml`](manifests/kube-state-metrics.yaml) | kube-state-metrics v2.20.0，命名空间改为 `vigil-monitoring`，镜像按 digest 固定 |
| [`manifests/grafana.yaml`](manifests/grafana.yaml) | Grafana 12.3.1、只读查询代理、两个数据源和五张大盘 |

业务清单来源是上游 `GoogleCloudPlatform/microservices-demo` 的 commit `d138db567079e2ef982d46b2993f8349cb18e2b2`（v0.9.0）。清单不含 Namespace 对象，部署时用 `-n vigil-shop` 指定。`frontend-external` 是 LoadBalancer 类型，本集群禁用了 servicelb，因此该 Service 一直处于 pending；访问前端使用 port-forward。

业务镜像均按 digest 固定，tag 仅用于说明来源。

| 工作负载 | 上游 tag | digest |
|---|---|---|
| adservice | v0.9.0 | `sha256:7a62fd95ef19b9379fbb4f8e3523ca2aa417a4039d336ef3246dca91624c60f9` |
| cartservice | v0.9.0 | `sha256:b0cf3662e60750e9a449fe14f5bc29f9150db0c553e52795569c477ec783974b` |
| checkoutservice | v0.9.0 | `sha256:b660e1a48bfd95854fb769113dd453811bb8470b7b58b420efead6fad5b5df61` |
| currencyservice | v0.9.0 | `sha256:16d16968f90aff24d4170ca7c4c0ff97303612b98c509052d5c3618f6e75937a` |
| emailservice | v0.9.0 | `sha256:93e25b258931d3148b1de07dc147a557af08153b75c2f1184bcc505cbf20060d` |
| frontend | v0.9.0 | `sha256:68f72fd525591c4879fb28fa9f81740ccfb7b1303e965f6dbc0ea43dbf2023fc` |
| loadgenerator | v0.9.0 | `sha256:b8542fe077d6e21dc03ca9d37a863e29140eb1af00ebb5d645c3ec05a093e5a8` |
| paymentservice | v0.9.0 | `sha256:ddbec6dd0efd0d34c8d8a6de3c52aad299ec6848fdd43d46cc6d6032ef1ff7c7` |
| productcatalogservice | v0.9.0 | `sha256:1d51f9e5aa923ff9c338b1b155b39da737306c0ebc230e62ec6271f2b0cb7bef` |
| recommendationservice | v0.9.0 | `sha256:200c10a2f26d77c98e6c6aa32ada517b91de8e75188e66cafc13f7421dd4c408` |
| redis-cart | alpine | `sha256:21090af93f8324db2f0f0f4f38a7c60369d47d917e000c3423d89d2ae2883537` |
| shippingservice | v0.9.0 | `sha256:c906f4bdba49226494bdf3461ab790f878e53329ba3f9e2edde95a14581268b1` |
| loadgenerator 的 init 容器 | latest | `sha256:a46257e06c82ad85242d0d38077d0a52d9896389ca60a1a59d3463db586e5fa4` |

观测组件：

| 组件 | 版本 | digest |
|---|---|---|
| Prometheus | v3.15.1 | `sha256:fa3302279710be2f21a8301ec17a8b4f2301fd829bd113839b3b6b02b6f191a2` |
| AlertManager | v0.34.2 | `sha256:39d8bb3966c694a124428a3a88fbcffd40bd069129ecd47c059f8a3f17c55f3b` |
| blackbox-exporter | v0.29.1 | `sha256:ea1c7aa3ef3f307cd03e85f34abb604478ccb90b567fc690da0cd9776da76b13` |
| kube-state-metrics | v2.20.0 | `sha256:42cfe3723a5f058171c627537fb57a3ea0f26e4380fa18555a95cb1a1b4cfc5b` |

## 3. 从干净资源启动

以下命令是本次实际执行路径的复现。创建集群、部署工作负载和注入故障属于写操作，只应在获准的实验环境执行。

```bash
k3d cluster create vigil-lab \
  --servers 1 --agents 0 \
  --api-port 127.0.0.1:6443 \
  --k3s-arg '--disable=traefik@server:0' \
  --k3s-arg '--disable=servicelb@server:0' \
  --no-lb \
  --kubeconfig-update-default=false \
  --kubeconfig-switch-context=false \
  --timeout 10m --wait
```

```bash
mkdir -p "$HOME/.kube"
k3d kubeconfig get vigil-lab > "$HOME/.kube/vigil-lab.yaml"
chmod 600 "$HOME/.kube/vigil-lab.yaml"
export KUBECONFIG="$HOME/.kube/vigil-lab.yaml"
```

```bash
kubectl create namespace vigil-shop --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -n vigil-shop -f env/manifests/online-boutique-pinned.yaml
kubectl wait --for=condition=Available deployment --all -n vigil-shop --timeout=900s
```

```bash
kubectl apply -f env/manifests/monitoring.yaml
kubectl apply -f env/manifests/kube-state-metrics.yaml
kubectl wait --for=condition=Available deployment --all -n vigil-monitoring --timeout=600s
```

首次部署需要拉取 17 个工作负载镜像。拉取耗时未单独记录，启动总耗时因此不作承诺。宿主机有余量时，等待窗口内可以完成。

## 4. 基线核查

| 检查 | 实测结果 |
|---|---|
| 浏览首页 | HTTP 200 |
| 商品页 | HTTP 200 |
| 加入购物车 | HTTP 302 |
| 购物车页 | HTTP 200，显示 Sunglasses、SKU #OLJCESPC7Z、数量 1、合计 $28.98 |
| loadgenerator | 10 用户，首轮 359 次请求、0 失败 |
| Prometheus 目标 | 5 个 job 全部 UP：prometheus、alertmanager、blackbox-exporter、kube-state-metrics、frontend-probe |
| 黑盒探测 | `probe_success=1`，2 分钟 8/8 成功，平均 8.1 ms，最大 8.9 ms |
| kube-state-metrics | `vigil-shop` 12 个 Deployment 可用 |
| 工作负载 | 业务 12 个、监控 4 个 Deployment 全部 1/1 |

前端访问：

```bash
kubectl port-forward -n vigil-shop svc/frontend 8080:80 --address 127.0.0.1
```

Prometheus 与 AlertManager 同样只通过 port-forward 访问：

```bash
kubectl port-forward -n vigil-monitoring svc/prometheus 9090:9090 --address 127.0.0.1
```

## 5. 已定位的配置问题

1. **blackbox-exporter 配置文件名不匹配。** ConfigMap 的键是 `blackbox.yml`，容器默认读取 `config.yml`。启动参数必须写成 `--config.file=/etc/blackbox_exporter/blackbox.yml`，否则进程反复退出。
2. **Prometheus 的 RWO 数据卷不能滚动更新。** 默认 RollingUpdate 会让新旧 Pod 同时挂载同一个 PVC，新 Pod 报 `lock DB directory: resource temporarily unavailable` 并崩溃。Deployment 必须设置 `strategy.type: Recreate`；修改策略后需先缩到 0 再重建。
3. **`ndots:5` 影响集群内域名解析。** Pod 的 `/etc/resolv.conf` 是 `ndots:5`，不带尾点的长域名会先拼接搜索域并解析到错误地址，Prometheus 抓取因此返回 EOF。同命名空间目标使用短名称，跨命名空间使用带尾点的绝对域名，例如 `frontend.vigil-shop.svc.cluster.local.`。
4. **AlertManager 投递受同一解析问题影响。** Prometheus 的 `alerting.alertmanagers` 最初写成不带尾点的长域名，结果规则已 firing 而 AlertManager 收到 0 条。改为同命名空间的 `alertmanager:9093` 后恢复投递，规则和 `for` 未改。

## 6. 边界

- 观测清单包括 Prometheus、blackbox-exporter、kube-state-metrics、AlertManager 和 Grafana。
- 实验镜像在本地构建，通过 `k3d image import` 导入，没有推送镜像仓库。
- 集群创建、清单部署和故障注入都是写操作。本文只记录执行路径，不构成再次执行的授权。

## 7. 实验 PostgreSQL

独立实例，承载业务表与 LangGraph checkpoint。它不在 k3d 集群内，也不复用宿主已有的其他项目数据库。

| 项目 | 实测值 |
|---|---|
| 容器 | `vigil-postgres` |
| 镜像 | `pgvector/pgvector:pg16`（本机已有，未额外拉取） |
| 版本 | PostgreSQL 16.15 |
| 端口 | `127.0.0.1:55432`，只绑定回环；宿主 5432 已被其他项目占用 |
| 卷 | `vigil-pgdata` |
| 用户／库 | `vigil` / `vigil` |
| 连接串 | `.env` 中的 `VIGIL_DATABASE_URL`，不写入仓库 |

业务表迁移在 `src/aiops/migrations/`，可重复执行；LangGraph checkpoint 表由官方 saver 管理，不在这组迁移中。迁移与连接检查原本靠旧 CLI 完成，入口随命令行一起重做，见设计文档。

这是单机实验实例，没有备份、副本或连接池容量规划，不构成生产建议。

## 8. 告警入口

AlertManager 通过 webhook 把 firing 告警交给 Vigil 的接收端。接收端只保留白名单标签，`scenario` 等实验标记不会进入事件。

| 项目 | 实测值 |
|---|---|
| 接收地址 | `http://172.20.0.1:8091/webhook/alertmanager` |
| 172.20.0.1 | k3d 网络网关，即宿主在 `k3d-vigil-lab` 网络上的地址；换机器要重新确认 |
| 鉴权 | `Authorization: Bearer <token>`，token 存于 Secret `vigil-webhook-token` 的 `token` 键 |
| 路由 | `alertname="FrontendProbeFailed"` 走 `vigil-webhook`，其余仍为 `default` |
| 去重 | 同一告警指纹在 `VIGIL_DEDUP_MINUTES`（默认 10 分钟）内只建一次事件 |

宿主地址可用下面的命令确认，不要把 172.20.0.1 当成固定值：

```bash
docker inspect k3d-vigil-lab-server-0 --format '{{range $n, $net := .NetworkSettings.Networks}}{{$n}} gw={{$net.Gateway}}{{"\n"}}{{end}}'
```

启动接收端前先建 Secret，token 从 `.env` 读取，不写入仓库：

```bash
kubectl create secret generic vigil-webhook-token -n vigil-monitoring \
  --from-literal=token="$(grep '^VIGIL_WEBHOOK_TOKEN=' .env | cut -d= -f2-)"
```

接收端的启动入口随命令行一起重做，见设计文档；启动参数仍读 `VIGIL_WEBHOOK_HOST`、`VIGIL_WEBHOOK_PORT`。

接收端运行期间保持 Prometheus 的 port-forward 在线。

## 9. 删除集群

删除整个实验环境是破坏性操作，需要用户明确授权：

```bash
k3d cluster delete vigil-lab
```

该命令会删除集群容器、网络和 `k3d-vigil-lab-images` 镜像卷。`~/.kube/vigil-lab.yaml` 不会被自动删除，需要单独清理。

## 10. 值班数据与 Grafana

在本次隔离实验的 kubeconfig 和上下文下部署。以下变量取本次环境的实际值。

```bash
kubectl --kubeconfig "$VIGIL_KUBECONFIG" --context "$VIGIL_KUBE_CONTEXT" \
  apply -f env/manifests/monitoring.yaml -f env/manifests/kube-state-metrics.yaml \
  -f env/manifests/grafana.yaml
kubectl --kubeconfig "$VIGIL_KUBECONFIG" --context "$VIGIL_KUBE_CONTEXT" \
  -n vigil-monitoring rollout status deployment/vigil-grafana
kubectl --kubeconfig "$VIGIL_KUBECONFIG" --context "$VIGIL_KUBE_CONTEXT" \
  -n vigil-monitoring port-forward svc/vigil-grafana 3000:3000 --address 127.0.0.1
```

更新已有实验部署时，修改 Prometheus 配置后重载配置。Grafana 的配置卷更新后重建 Grafana Pod。Grafana 使用匿名 Viewer，实验环境通过本机端口转发访问。数据源代理只允许指定的 HTTP 查询，指标和告警都不能通过 Grafana 修改。

值班服务的 `VIGIL_PROMETHEUS_URL` 和 `VIGIL_ALERTMANAGER_URL` 默认分别是本机端口 9090、9093。为 AlertManager 启用相应的本机端口转发。设置 `VIGIL_GRAFANA_URL=http://127.0.0.1:3000` 后网页提供五个跳转，访问子路径部署时将子路径也写入该地址。

`vigil-entry-probe` 抓取首页、商品详情与购物车，使用 `application` 和 `address` 标签。单独保留 `frontend-probe` 的首页告警和回滚核实。集群权限仅授予实验 Prometheus 的 ServiceAccount。它发现节点后以 TLS 和短期 ServiceAccount 令牌直接读取 kubelet 的 `/metrics/cadvisor`，只留工作负载容器 CPU 和内存样本。值班面板的当前用量来自 Kubernetes `metrics.k8s.io`。

在宿主机另起发布指标进程。该进程使用本次实验的 `VIGIL_KUBECONFIG`、`VIGIL_KUBE_CONTEXT`、`VIGIL_CLUSTER`、`VIGIL_NAMESPACE` 与 `VIGIL_DATABASE_URL`。先确认网关地址对应本次 k3d 网络，再绑定该地址。

```bash
uv run --frozen python -m aiops.monitoring.exporter --host "$VIGIL_LAB_GATEWAY" --port 8092
```

Prometheus 从 `host.k3d.internal:8092/metrics` 抓取发布指标。确认本次集群的该主机名解析到进程绑定的地址；若使用其他实验网关映射，同步修改 `vigil-releases` 的抓取目标。监控命名空间和业务命名空间均以实验清单为准，服务大盘查询 `vigil-shop`。

在本次实验的 Prometheus 中逐项检查：

| 查询 | 应看到的结果 |
|---|---|
| `probe_success{job="frontend-probe"}` | 首页只有一条序列，首页故障仍由原规则判断 |
| `probe_success{job="vigil-entry-probe"}` | 业务首页、商品页和购物车各一条，带应用与地址标签 |
| `up{job="kubelet-cadvisor"}` | 每个实验节点抓取成功 |
| `container_cpu_usage_seconds_total{job="kubelet-cadvisor",namespace="vigil-shop"}` | 工作负载容器 CPU 样本 |
| `container_memory_working_set_bytes{job="kubelet-cadvisor",namespace="vigil-shop"}` | 工作负载容器工作集内存样本 |
| `vigil_release_source_available` | 发布历史和动作记录分别报来源状态 |
| `vigil_release_timestamp_seconds` | 24 小时内保留的发布与已核实回滚，含服务、版本、类型及时间 |

在五张 Grafana 大盘核对对应查询。服务大盘按 Deployment 展示实验业务容器，节点大盘按节点汇总抓到的工作负载容器。暂停容器采集或发布出口时，核对相应大盘的缺数状态。告警恢复通知需与新版告警接收端同次切换，接点见 [值班数据接口](../docs/MONITORING.md)。
