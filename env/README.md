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

AlertManager 通过 webhook 投递告警触发和恢复通知。Vigil 按推送中的 `groupKey` 归并，整组恢复后关闭会话。

| 项目 | 实测值 |
|---|---|
| 接收地址 | `http://172.20.0.1:8091/webhook/alertmanager` |
| 172.20.0.1 | k3d 网络网关，即宿主在 `k3d-vigil-lab` 网络上的地址；换机器要重新确认 |
| 鉴权 | `Authorization: Bearer <token>`，token 存于 Secret `vigil-webhook-token` 的 `token` 键 |
| 路由 | `alertname="FrontendProbeFailed"` 走 `vigil-webhook`，其余仍为 `default` |
| 会话 | 同一组最多一个未关闭的会话。重复触发只追加消息，关闭后再次触发会新建会话 |

宿主地址可用下面的命令确认，不要把 172.20.0.1 当成固定值：

```bash
docker inspect k3d-vigil-lab-server-0 --format '{{range $n, $net := .NetworkSettings.Networks}}{{$n}} gw={{$net.Gateway}}{{"\n"}}{{end}}'
```

启动接收端前，用本地保存的令牌文件创建 Secret：

```bash
kubectl --kubeconfig "$VIGIL_KUBECONFIG" --context "$VIGIL_KUBE_CONTEXT" \
  create secret generic vigil-webhook-token -n vigil-monitoring \
  --from-file=token="$VIGIL_WEBHOOK_TOKEN_FILE"
```

停止旧接收进程后，配置 `VIGIL_WEBHOOK_HOST` 为确认的实验网关，
`VIGIL_WEBHOOK_PORT` 为接收端口，`VIGIL_WEBHOOK_TOKEN` 与 Secret 的内容一致。
运行 `vigil serve`，在同一次变更中更新 AlertManager 的接收地址、Bearer 文件和
`send_resolved: true`，重载配置后核对 firing 与 resolved 投递。

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

Prometheus 默认从 `host.k3d.internal:8092/metrics` 抓取发布指标。若集群内这个名称无法访问宿主进程，用本次 k3d 网络的网关地址渲染抓取目标。已运行的 Prometheus 要重新加载配置后才会使用新地址。

```bash
docker network inspect "k3d-$VIGIL_CLUSTER" --format '{{(index .IPAM.Config 0).Gateway}}'
# 将上一步确认的网关地址赋给 VIGIL_LAB_GATEWAY。
sed "s|host.k3d.internal:8092|${VIGIL_LAB_GATEWAY:?}:8092|" env/manifests/monitoring.yaml |
  kubectl --kubeconfig "$VIGIL_KUBECONFIG" --context "$VIGIL_KUBE_CONTEXT" apply -f -
```

监控命名空间和业务命名空间均以实验清单为准，服务大盘查询 `vigil-shop`。

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

## 11. 受控自动修复验收

### 建立带审计的隔离集群

在运行 Docker 的 Linux 宿主上，从仓库根目录创建独立集群。为本次实验选未占用的 API、Prometheus、Alertmanager 和 Vigil 端口。

```bash
lab=vigil-remediation-lab
mkdir -p .evidence
run_dir=$(mktemp -d "$PWD/.evidence/remediation.XXXXXXXX")
mkdir "$run_dir/audit"
chmod 700 "$run_dir"
k3d cluster create "$lab" --servers 1 --agents 0 --no-lb \
  --image rancher/k3s:v1.35.5-k3s1 \
  --api-port 127.0.0.1:6444 \
  --kubeconfig-update-default=false --kubeconfig-switch-context=false \
  --volume "$PWD/env/manifests/audit-policy.yaml:/etc/k3s/vigil-audit.yaml:ro@server:0" \
  --volume "$run_dir/audit:/var/log/vigil-audit@server:0" \
  --k3s-arg '--disable=traefik@server:0' \
  --k3s-arg '--disable=servicelb@server:0' \
  --k3s-arg '--kube-apiserver-arg=audit-policy-file=/etc/k3s/vigil-audit.yaml@server:0' \
  --k3s-arg '--kube-apiserver-arg=audit-log-path=/var/log/vigil-audit/audit.jsonl@server:0' \
  --k3s-arg '--kube-apiserver-arg=audit-log-maxsize=100@server:0' \
  --k3s-arg '--kube-apiserver-arg=audit-log-maxbackup=5@server:0' \
  --timeout 10m --wait
export VIGIL_LAB_KUBECONFIG="$run_dir/operator.kubeconfig"
export VIGIL_LAB_CONTEXT="k3d-$lab"
k3d kubeconfig get "$lab" >"$VIGIL_LAB_KUBECONFIG"
chmod 600 "$VIGIL_LAB_KUBECONFIG"
k() { kubectl --kubeconfig "$VIGIL_LAB_KUBECONFIG" --context "$VIGIL_LAB_CONTEXT" "$@"; }
k get nodes
k auth whoami
```

在隔离集群安装 Online Boutique、监控和专用身份。设定 `VIGIL_WEBHOOK_TOKEN_FILE` 为本地令牌文件，`VIGIL_LAB_WEBHOOK_PORT` 为 Vigil 告警端口。Vigil 监听本次 k3d 网关可访问的地址。

```bash
k create namespace vigil-shop --dry-run=client -o yaml | k apply -f -
k create namespace vigil-monitoring --dry-run=client -o yaml | k apply -f -
k -n vigil-monitoring create secret generic vigil-webhook-token \
  --from-file=token="${VIGIL_WEBHOOK_TOKEN_FILE:?}"
k apply -n vigil-shop -f env/manifests/online-boutique-pinned.yaml
k rollout status -n vigil-shop deployment/frontend --timeout=600s
bash faults/f04-replica-capacity/repeat.sh install
bash faults/f05-service-config/repeat.sh install
k apply -f env/manifests/kube-state-metrics.yaml
gateway=$(docker network inspect "k3d-$lab" --format '{{(index .IPAM.Config 0).Gateway}}')
export VIGIL_LAB_WEBHOOK_URL="http://$gateway:${VIGIL_LAB_WEBHOOK_PORT:?}/webhook/alertmanager"
sed "s|http://172.20.0.1:8091/webhook/alertmanager|$VIGIL_LAB_WEBHOOK_URL|" \
  env/manifests/monitoring.yaml | k apply -f -
k -n vigil-monitoring port-forward svc/prometheus 9090:9090 --address 127.0.0.1
# 在独立终端执行。
k -n vigil-monitoring port-forward svc/alertmanager 9093:9093 --address 127.0.0.1
```

实际转发端口有变动时，导出 `VIGIL_LAB_PROMETHEUS_URL` 和 `VIGIL_LAB_ALERTMANAGER_URL`。Vigil 写入使用专用身份 `system:serviceaccount:vigil-monitoring:vigil-remediation`，注入、复位和采集使用操作者身份。先执行预期被拒的专用身份 dry-run，核对审计有 `RequestReceived` 与终态。随后核对各业务探针基线和 cAdvisor 指标。在每次场景开始前记录 Vigil 版本、启用的策略版本和 UTC 开始时间。

### 启用受控写入

先应用与策略版本对应的授权和准入清单。使用补偿夹具时，给生成器传入
`--config-dir config/remediation/isolated-acceptance --output-dir "$run_dir/manifests"`，
应用生成的两份清单，并让 Vigil 读取同一个策略目录。每次更换策略后重启 Vigil。

```bash
uv run --frozen python scripts/generate_remediation_manifests.py --check
k apply -f env/manifests/remediation-rbac.yaml -f env/manifests/remediation-admission.yaml
export VIGIL_ACTION_KUBECONFIG="$run_dir/action.kubeconfig"
export VIGIL_ACTION_KUBE_CONTEXT=vigil-remediation
umask 077
k config view --minify --flatten --raw -o json |
  python3 -c 'import json,sys; config=json.load(sys.stdin); config["users"]=[]; config["contexts"]=[]
json.dump(config,sys.stdout)' >"$VIGIL_ACTION_KUBECONFIG"
action_k() { kubectl --kubeconfig "$VIGIL_ACTION_KUBECONFIG" "$@"; }
k -n vigil-monitoring create token vigil-remediation --duration=4h |
  python3 -c '
import json, pathlib, sys
path, name = pathlib.Path(sys.argv[1]), sys.argv[2]
config = json.loads(path.read_text())
token = sys.stdin.read().strip()
if not token:
    raise SystemExit("未取得专用令牌")
config["users"] = [{"name": name, "user": {"token": token}}]
config["contexts"] = [{"name": name, "context": {
    "cluster": config["clusters"][0]["name"], "user": name,
}}]
config["current-context"] = name
path.write_text(json.dumps(config))
' "$VIGIL_ACTION_KUBECONFIG" "$VIGIL_ACTION_KUBE_CONTEXT"
action_k auth whoami
```

在独立终端设置实验库的 `VIGIL_DATABASE_URL`、告警监听地址和令牌后，运行以下命令。
读取工具的访问另由 `VIGIL_KUBECONFIG` 配置。

```bash
export VIGIL_CLUSTER=vigil-lab
export VIGIL_REMEDIATION_CONFIG_DIR="$PWD/config/remediation"
export VIGIL_REMEDIATION_AUTO_ENABLED=true
export VIGIL_PROMETHEUS_URL="${VIGIL_LAB_PROMETHEUS_URL:-http://127.0.0.1:9090}"
export VIGIL_ALERTMANAGER_URL="${VIGIL_LAB_ALERTMANAGER_URL:-http://127.0.0.1:9093}"
uv run --frozen vigil admin migrate
uv run --frozen vigil serve
```

确认写入身份可读登记对象。用该身份对登记 Pod 提交修改标签的服务端 dry-run，
核对 RBAC 允许 `patch`，以及准入拒绝实际修改。对登记工作负载的允许字段也做一次
服务端 dry-run，核对拒绝和允许两个请求均进入审计。

### 运行六条场景

在已准备好实验 kubeconfig 和端口转发的终端定义命令。

```bash
f02() { bash faults/f02-memory-limit-oom/repeat.sh "$@"; }
f04() { bash faults/f04-replica-capacity/repeat.sh "$@"; }
f05() { bash faults/f05-service-config/repeat.sh "$@"; }
alert() { python3 scripts/remediation_lab_alert.py "$@"; }
capture() { bash scripts/capture_remediation_lab.sh "$@"; }
stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }
```

每个场景使用独立目录。将 `case_dir` 设为 `"$run_dir/01-oom"` 等对应路径，运行 `mkdir "$case_dir"` 和 `stamp >"$case_dir/since.txt"`，再采集基线。 `silence` 返回一个 `silenceID`，交给 `expire` 恢复发送。等待夹具探针和告警进入预期状态后再运行 `verify`。先采集 Vigil 终态、资源和审计，再复位。

1. **内存自动修复。** 运行 `capture f02 "$case_dir/baseline"`。用 `alert silence FrontendProbeFailed --source frontend` 延迟告警投递。运行 `f02 inject`、`f02 verify`，保存故障快照及 `alert capture FrontendProbeFailed --source frontend >"$case_dir/firing.json"`。解除 silence。核对 Vigil 只改内存 limit 到 `128Mi`，容器就绪、首页重新通过、对应告警恢复后继续观察。10 个新鲜的分钟时段均通过才可记成功。审计仅有一次真实 PATCH。取证后运行 `f02 restore`。

   **CPU 变体。** 创建独立证据目录和审计时间段。节点需启用 CPU 配额。暂缓 `FrontendProbeFailed` 投递，运行 `f02 cpu-load`。检查正常规格下探针、实际负载和两条限流计数，确认限流比例稳定低于 0.5。运行 `f02 cpu-inject`，随后运行 `f02 cpu-verify`。如需先保存故障状态，也给 `VigilCpuLimitDegraded` 单独设一个 silence，采集后解除这个 CPU silence。等 Vigil 将 CPU limit 改回 `200m`，核对固定负载仍在、业务恢复、当前容器限流回到阈值以下。只接受一次真实 CPU 规格 PATCH。采完证据才运行 `f02 cpu-stop` 和 `f02 restore`，等首页告警消失后再解除首页 silence。

2. **缺少 OOM 证据。** 保持首页告警被 silence，运行 `f02 inject-insufficient` 和 `f02 verify-insufficient`。捕获未送达的真实通知形状，再用新标识投递。Vigil 应记录低限额及业务故障，指出当前同模板容器缺少 OOM 证据，进入人工处理。保留可能生成的卡片且不批准。审计以专用身份的全部写请求为范围，真实写入为零。

   ```bash
   alert capture FrontendProbeFailed --source frontend >"$case_dir/actual-alert.json"
   alert replay "$VIGIL_LAB_WEBHOOK_URL" "$case_dir/actual-alert.json" \
     --token-file "$VIGIL_WEBHOOK_TOKEN_FILE" --mode new >"$case_dir/injected-webhook.json"
   ```

   采集完成后运行 `f02 restore`，等告警消失再解除 silence。

3. **改坏后补偿。** 启用 f05 ConfigMap 候选为 `worsen` 的独立实验策略。给 `VigilServiceConfigDegraded` 的 `configmap` 来源设置 silence。运行 `f05 inject configmap compensate`、`f05 verify configmap`。确认 `degraded` 已加载满五分钟，用 `f05 samples configmap` 保存原始五分钟数据。连续五个业务时段恰有一次失败后解除 silence。等写入和应用加载 `worsen`，保存生效头五分钟的五次失败，核对 Vigil 只补偿一次并把 `data.mode` 恢复到 `degraded`。账本显示已补偿且留下原故障待办。结束前再触发同一故障的重复投递，核对仍只有处置和补偿各一次真实 PATCH。取证后运行 `f05 restore configmap`。

   变体各用独立基线。用 `f05 inject configmap low-margin` 构造 1/5 到 2/5 的恶化，用 `f05 inject configmap` 构造 5/5 的基线。两者记录业务仍异常及人工结果。用 `f05 drift configmap` 在已写入候选、尚未补偿时改字段为 `limited`，核对不覆盖操作者值。测试准入拒绝时，用隔离的准入配置明确拒绝补偿请求，保留 dry-run 或真实拒绝记录，核对无第二次补偿且生成待办。

4. **实例数扩容。** 用 `alert silence VigilReplicaCapacityDegraded --source capacity` 暂缓投递。采集健康快照。运行 `f04 inject` 和 `f04 verify`。确认只有一个端点、每批两笔真实交易有一笔 429，保存故障快照后解除 silence。核对一次真实 PATCH 使副本数从 1 变为 2，两个实例分别承接交易。观察结束后采集写入和业务证据，再运行 `f04 restore`。用审批入口分别提交正数缩容、低于 AUTO 下界的正数、0、超过硬上限和同值变体。保持合法审批未批准，核对风险归类与零额外真实写入。

5. **普通配置修复。** 使用候选为 `healthy` 的正常策略。依次对 `configmap` 和 `env` 运行 `f05 inject <source>`、`f05 verify <source>` 和 `f05 status <source>`。如需保存故障状态，按来源暂缓告警直至取证完成。每次核对一个字段的真实 PATCH、实际加载的对应值和业务恢复。在前一来源采完证据、复位并清除告警后，再测试另一来源。用实际审批入口提交普通值 `limited`，先核对无批准时零真实写入，再批准并记录卡片、资源值和业务状态。分别提交敏感字段和 `f05 share` 后的共享 ConfigMap 提案，核对拒绝且无卡片。采完证据后运行 `f05 unshare`。

6. **观察中再次异常。** 按场景 1 启动，在首页恢复且匹配的 `resolved` 已到 Vigil 时核对会话仍在观察。在满 10 分钟前运行 `f02 relapse`。记录新一轮首页失败、同类告警及该次处置不得成功。根据前后业务样本，明确恶化时至多补偿一次，其余进入人工处理。用 `alert replay --mode verbatim` 重放原通知，用 `--mode resolved` 重放原身份恢复通知，核对重复和晚到消息无额外授权。丢样变体另跑一次，在观察期用操作者身份将 Prometheus 副本暂时缩至 0，让至少一个完整分钟没有样本，再恢复到 1。保留告警、样本缺口和人工结果。最后运行 `f02 end-relapse` 和 `f02 restore`。

### 封存审计和业务证据

用 `capture f02|f04|f05 新目录` 采集动作前后和关键中间状态。该脚本保存资源 UID、版本、业务和告警样本、部署状态及限定字段。原始 Prometheus range-vector 样本带真实抓取时间。分别收集 Vigil 告警会话及执行账本，记录告警指纹、开始和恢复时间、正向与补偿 ID、观察状态及终态。

终态到达后记录 UTC `until`。读取仍在写入的审计文件前确认目标请求的终态已落盘。包含本次区间涉及的所有轮转日志。聚合时以请求开始时间落在 `[since, until)` 为准，终态可以在 `until` 后到达。

```bash
stamp >"$case_dir/until.txt"
operator=$(k auth whoami -o jsonpath='{.status.userInfo.username}')
set -o pipefail
docker exec "k3d-$lab-server-0" sh -c 'cat /var/log/vigil-audit/*.jsonl' |
  python3 scripts/collect_remediation_audit.py \
    --since "$(cat "$case_dir/since.txt")" --until "$(cat "$case_dir/until.txt")" \
    --actor "$operator" - >"$case_dir/audit-summary.json"
```

如有其他位置的轮转日志，一并传给采集脚本。采集按 audit ID 合并各阶段，保留 URI、目标、操作者和有效身份、dry-run、拒绝、失败、超时及无终态请求。核对 `summary` 的专用身份计数，同时逐条查看 `requests` 的目标与真实动词。资源实际字段差异用账本和快照核验。操作者复位单独记录时间及审计，证据与凭据均留在本地。
