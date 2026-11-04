# 本地实验环境

状态：2026-11-04 在当前开发机完成一次真实部署，并完成 F01 坏发布的注入、告警、操作者回退与复位。本文记录实际使用的版本、清单、步骤和实测结果，供同机复现与回查。这是单节点实验环境，不是生产环境，也不是其他机器的容量承诺。

范围见 [MVP 运行环境](../docs/mvp.md#2-运行环境与已知基线)，近期顺序见[迭代计划](../docs/iterations.md#2-当前切片-i01证明环境与坏发布实验可观测可复位)。F01 场景和运行记录见 [`faults/f01-bad-release/`](../faults/f01-bad-release/README.md)。

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
| Python | 3.12.14（pyenv），仅用于本地查询脚本，运行栈尚未安装 |

k3d 与 kubectl 安装在 `~/.local/bin`，安装包均校验 SHA-256。

## 2. 固化清单

| 文件 | 内容 |
|---|---|
| [`manifests/online-boutique-pinned.yaml`](manifests/online-boutique-pinned.yaml) | 上游 Online Boutique v0.9.0 的全量 12 个 Deployment 与 12 个 Service；只把镜像从 tag 替换为 digest |
| [`manifests/monitoring.yaml`](manifests/monitoring.yaml) | Prometheus、AlertManager、blackbox-exporter、配置、`FrontendProbeFailed` 规则和 Prometheus PVC |
| [`manifests/kube-state-metrics.yaml`](manifests/kube-state-metrics.yaml) | kube-state-metrics v2.20.0，命名空间改为 `vigil-monitoring`，镜像按 digest 固定 |

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

只读调查需要 Prometheus 转发在位，并读取 `.env` 中的 `VIGIL_DATABASE_URL`、`VIGIL_KUBECONFIG` 和 `VIGIL_MODEL_NAME`：

```bash
uv run --frozen aiops investigate --service frontend --symptoms "首页探测是否持续成功" --lookback-minutes 10
```

该命令会写业务表与 checkpoint 表，但不修改集群。

## 5. 资源实测

2026-11-04T09:28Z 快照：

| 项目 | 实测值 |
|---|---|
| 节点可分配 | 12 CPU，约 15.4 GiB 内存，110 个 Pod |
| 节点实际占用 | 142m CPU，1292 MiB 内存 |
| 业务 Pod 合计 | 约 476 MiB |
| 监控 Pod 合计 | 约 132 MiB |
| 宿主机可用内存 | 约 6.3 GiB |
| Prometheus 数据卷 | 2Gi PVC，实测数据约 1.1 MiB |
| 集群容器重启 | 业务与监控工作负载均为 0 重启 |

这是单次快照。宿主机上还有其他项目在运行，可用内存不等于本集群可长期占用的余量。`kubectl top` 依赖 k3s 自带的 metrics-server。

## 6. 已定位的配置问题

1. **blackbox-exporter 配置文件名不匹配。** ConfigMap 的键是 `blackbox.yml`，容器默认读取 `config.yml`。启动参数必须写成 `--config.file=/etc/blackbox_exporter/blackbox.yml`，否则进程反复退出。
2. **Prometheus 的 RWO 数据卷不能滚动更新。** 默认 RollingUpdate 会让新旧 Pod 同时挂载同一个 PVC，新 Pod 报 `lock DB directory: resource temporarily unavailable` 并崩溃。Deployment 必须设置 `strategy.type: Recreate`；修改策略后需先缩到 0 再重建。
3. **`ndots:5` 影响集群内域名解析。** Pod 的 `/etc/resolv.conf` 是 `ndots:5`，不带尾点的长域名会先拼接搜索域并解析到错误地址，Prometheus 抓取因此返回 EOF。同命名空间目标使用短名称，跨命名空间使用带尾点的绝对域名，例如 `frontend.vigil-shop.svc.cluster.local.`。
4. **AlertManager 投递受同一解析问题影响。** Prometheus 的 `alerting.alertmanagers` 最初写成不带尾点的长域名，结果规则已 firing 而 AlertManager 收到 0 条。改为同命名空间的 `alertmanager:9093` 后恢复投递，规则和 `for` 未改。

## 7. 边界与未完成

- 仓库还没有逐步截图和原始查询导出，证据以文本记录和清单为主。
- 只记录了一次部署的资源快照，没有做容量规划或长时间稳定性测试。
- 没有部署 Loki、Tempo、OTel Collector；F01 只使用 Prometheus、blackbox、kube-state-metrics 和 AlertManager。
- 坏发布镜像由本地构建并通过 `k3d image import` 导入，没有推送镜像仓库。
- 实验 PostgreSQL 16.15 已启动并完成业务表迁移；LangGraph checkpoint 的真实读写仍未验证。
- 集群创建、清单部署和故障注入都是写操作。本文只记录已执行过的路径，不构成再次执行的授权。

## 8. 实验 PostgreSQL

2026-11-04 启动独立实例，承载业务表迁移。它不在 k3d 集群内，也不复用宿主已有的其他项目数据库。

| 项目 | 实测值 |
|---|---|
| 容器 | `vigil-postgres` |
| 镜像 | `pgvector/pgvector:pg16`（本机已有，未额外拉取） |
| 版本 | PostgreSQL 16.15 |
| 端口 | `127.0.0.1:55432`，只绑定回环；宿主 5432 已被其他项目占用 |
| 卷 | `vigil-pgdata` |
| 用户／库 | `vigil` / `vigil` |
| 连接串 | `.env` 中的 `VIGIL_DATABASE_URL`，不写入仓库 |

迁移与连接检查：

```bash
uv run --frozen aiops db migrate
uv run --frozen aiops db status
```

业务表定义在 `src/aiops/migrations/0001_init.sql`，包含 `incidents`、`investigations`、`hypotheses`、`tool_calls`、`evidence`、`audit_events` 六张表和 `schema_migrations` 版本表，迁移可重复执行。LangGraph 官方 checkpoint 表尚未接入，接入后由 saver 自行管理，不并入这组迁移。

这是单机实验实例，没有备份、副本或连接池容量规划，不构成生产建议。

## 9. 告警入口

AlertManager 通过 webhook 把 firing 告警交给 `aiops serve`。接收端只保留白名单标签，`scenario` 等实验标记不会进入事件。

| 项目 | 实测值 |
|---|---|
| 接收地址 | `http://172.20.0.1:8081/webhook/alertmanager` |
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

```bash
uv run --frozen aiops serve --host 172.20.0.1 --port 8081
```

调查仍需要 Prometheus 的 port-forward 在位。收到告警后服务先返回 202，再异步发起调查；`aiops incidents` 查看事件，`aiops report <investigation_id>` 查看结论与证据统计。

2026-11-04 实测：经 AlertManager 的 v2 API 投递一条带 `scenario` 标签的测试告警，接收端返回 202，建立事件 `inc-4845d5099c7c` 与调查 `inv-42f562005c5f`；事件上下文不含 `scenario`；同一指纹再次投递返回 `deduplicated`。调查结论为 `inconclusive`，因为真实探测当时健康，假设被证据反驳。

## 10. 受控回退

`aiops act` 只创建提案并给出风险判定，`aiops approve` 才执行。执行前重新读取目标，比对 UID 与 resourceVersion；滚动完成后再观察业务探测的末段连续样本。

```bash
uv run --frozen aiops act --deployment productcatalogservice --reason "前端探测持续失败" --actor <name>
uv run --frozen aiops approvals
uv run --frozen aiops approve <proposal_id> --actor <name>
```

| 项目 | 实测值 |
|---|---|
| 白名单 | 只允许 `vigil-shop` 下的 `rollout_undo` |
| 审批 | 首次执行一律需人工审批，规则不接受模型自评分 |
| 回退目标 | 当前 revision 的前一 revision 的 Pod 模板，`resourceVersion` 作为并发前置 |
| 并发 | `action_locks` 保证同一目标同时只有一个执行 |
| 幂等 | 同一目标版本只允许一个进行中的提案；失败或拒绝后可重新提案 |
| 业务验证 | 滚动完成后等两个抓取周期，窗口 90 秒、步长 15 秒；末段 3 个样本必须全为 1 |
| 未知结果 | 写入异常记为 `unknown`，停止且不重放 |

2026-11-04 实测：把 `productcatalogservice` 切到实验坏镜像（revision 16），探测失败后提案 `act-b5be04095a56`，审批后回退到 revision 15，滚动完成，窗口内 7 个样本全部为 1，状态 `verified`；集群恢复健康 digest，`probe_success=1`。

同一天的前四次尝试都是失败记录，保留在库中：

1. `act-e2edf74326b2`：dry-run 返回 HTTP 500。原因是 ReplicaSet 模板用 `to_dict()` 得到蛇形字段，`containerPort` 等合并键不匹配；改用 `sanitize_for_serialization`。
2. `act-cd220f97659d`、`act-a52cb91c1f80`：执行成功但验证窗口覆盖了滚动前的失败样本，判为失败。
3. `act-a384b3a40e42`：加了两个抓取周期的等待后仍有 1 个预热样本为 0，于是把判定改为末段连续样本。
4. 期间还修正了一次性约束：原先 `idempotency_key` 全局唯一，失败后无法重新提案，改为只约束进行中的提案。

## 11. 复位与删除

故障实验的复位由 F01 脚本完成，保留坏 ReplicaSet 作为证据，最终业务和监控工作负载保持健康。

删除整个实验环境是破坏性操作，需要用户明确授权：

```bash
k3d cluster delete vigil-lab
```

该命令会删除集群容器、网络和 `k3d-vigil-lab-images` 镜像卷。`~/.kube/vigil-lab.yaml` 不会被自动删除，需要单独清理。
