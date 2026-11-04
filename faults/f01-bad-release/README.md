# F01 坏发布导致业务请求失败

状态：2026-11-04 在 `vigil-lab` 完成四次有效实验运行，其中三次由入库脚本 `repeat.sh` 完成，真实业务失败、告警、操作者回退和复位均有记录。仓库内现有 `repeat.sh` 是实验驱动脚本，不是最终统一的 `inject/verify/cleanup` 接口。运行记录见 [`runs/2026-11-04.md`](runs/2026-11-04.md)。

## 1. 场景与真值

场景卡见 [MVP F01](../../docs/mvp.md#f01坏发布导致业务请求失败ms01-首切片)。

- 载体：`productcatalogservice`。
- 注入点：`ListProducts` 返回 `codes.Internal`，gRPC 健康检查仍返回 SERVING。
- 预期：新镜像可以启动并 Ready，frontend 首页返回 500，黑盒探测失败并触发告警，旧 Pod 不继续服务。
- 反向边界：商品页和购物车仍为 200，因为 `GetProduct` 未受影响。不能用“Pod Ready”或“镜像拉取失败但旧 Pod 继续服务”替代业务故障。

真值只保存在本目录和运行记录中，包括故障服务、函数、实验镜像和冻结参数。按 [`faults/README.md`](../README.md) 的规则，这些内容不进入调查模型可读范围。该访问隔离尚未实现代码，接入调查前必须补齐。

实验镜像已改成中性错误文本和中性标签：错误信息是 `product catalog unavailable`，镜像标签是 `vigil/productcatalogservice:canary-20261104`，不再出现 `bad`、`experimental` 字样。2026-11-04 早先的运行使用旧标签 `vigil/productcatalogservice-bad:exp-20261104-1`，记录保留原样。访问级隔离仍未实现：模型能不能读到场景目录由工具层决定，这一条还要补。

实验镜像公开标明来源，不声称来自真实事故。查询、告警、Kubernetes 写操作和业务请求都是真实链路。

## 2. 实验镜像

上游版本为 Online Boutique v0.9.0，commit `d138db567079e2ef982d46b2993f8349cb18e2b2`。

修改 `src/productcatalogservice/product_catalog.go` 中的 `ListProducts`：

```go
func (p *productCatalog) ListProducts(context.Context, *pb.Empty) (*pb.ListProductsResponse, error) {
	time.Sleep(extraLatency)

	return nil, status.Errorf(codes.Internal, "product catalog unavailable")
}
```

构建与导入：

```bash
docker build \
  --label org.vigil.experiment=release-regression \
  --label org.vigil.scenario=f01 \
  -t vigil/productcatalogservice:canary-20261104 \
  src/productcatalogservice

k3d image import vigil/productcatalogservice:canary-20261104 -c vigil-lab
```

- 镜像标签：`vigil/productcatalogservice:canary-20261104`。
- 本地镜像 ID：`sha256:711e2001cec2f30e01dc71ce95bf77ca0f2a0a88cb2221b1e9b863b3eed8b955`，大小 45 MB。
- 旧标签 `vigil/productcatalogservice-bad:exp-20261104-1`（ID `sha256:995ea004...`）只用于复现早先的运行。
- 健康镜像：`gcr.io/google-samples/microservices-demo/productcatalogservice@sha256:1d51f9e5aa923ff9c338b1b155b39da737306c0ebc230e62ec6271f2b0cb7bef`。
- 镜像只在本地构建和导入，没有推送外部仓库。

## 3. 冻结参数

以下规则在第一次脚本化有效运行前冻结，运行中不调整。手工运行 1 早于冻结，只用于首次可行性验证。

| 项目 | 取值 | 依据 |
|---|---|---|
| 故障规则 | `FrontendProbeFailed` | 冻结在 Prometheus 配置中 |
| 表达式 | `probe_success{job="frontend-probe",instance="http://frontend.vigil-shop.svc.cluster.local./"} == 0` | 黑盒 HTTP 探测，不依赖 Pod Ready |
| `for` | `1m` | 4 个 15 秒抓取周期 |
| 恢复判定 | `probe_success` 连续 60 秒为 1，同时首页返回 200，且 Prometheus 活动告警为 0 | 与故障窗口对称 |
| 抓取与评估间隔 | 15 秒 | Prometheus 全局配置 |
| 流量 | loadgenerator 10 用户持续运行 | 记录运行前后请求数与失败数 |
| 延迟 | `probe_duration_seconds` 只记录 | F01 的机制是可用性失败，不是延迟劣化 |
| 复位判定 | 健康镜像生效，坏 ReplicaSet 为 0 副本，没有坏 Pod 运行，活动告警为 0 | 坏 ReplicaSet 保留作为证据 |

这些数值只用于 F01 实验，不是通用 SLO。它们尚未写入可配置的场景参数文件。

## 4. 操作步骤

前置条件：`productcatalogservice` 使用健康 digest，`probe_success=1`，没有活动告警，坏 ReplicaSet 为 0 副本，前端可以通过 port-forward 访问。

注入：

```bash
kubectl set image deployment/productcatalogservice -n vigil-shop \
  server=vigil/productcatalogservice:canary-20261104
kubectl rollout status deployment/productcatalogservice -n vigil-shop --timeout=180s
```

核验：

```bash
kubectl get deploy productcatalogservice -n vigil-shop \
  -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
kubectl get rs,pods -n vigil-shop -l app=productcatalogservice
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/
```

同时查询 Prometheus 的 `probe_success` 和 `/api/v1/alerts`，确认探测为 0 且 `FrontendProbeFailed` 进入 firing。

操作者回退：

```bash
kubectl rollout undo deployment/productcatalogservice -n vigil-shop
kubectl rollout status deployment/productcatalogservice -n vigil-shop --timeout=180s
```

回退后按冻结的 60 秒窗口验证恢复，再确认活动告警清零。回退由实验操作者执行，单独记录，不计 Agent 处置成功。

复位：确认健康镜像、坏 ReplicaSet 0 副本、活动告警 0。坏 ReplicaSet 和实验镜像保留，便于回查。`kubectl rollout undo` 不会更新 `kubectl apply` 写入的 last-applied 注解；后续应用原始清单时会重新对齐。

## 5. repeat.sh

[`repeat.sh`](repeat.sh) 把上述步骤串成一次可重复运行，包含前置检查、注入、业务与告警核验、回退、60 秒恢复窗口、告警清零和最终状态输出。任何一步失败都会先尝试安全回退，再以非零状态退出。

```bash
KUBECONFIG="$HOME/.kube/vigil-lab.yaml" bash faults/f01-bad-release/repeat.sh
```

脚本会真实修改 `vigil-shop` 中的 Deployment 并注入故障，只能在专用实验集群运行。它依赖宿主机上的 `kubectl`、`curl` 和 `python3`，默认端口 8080 用于前端 port-forward。

脚本不是最终场景接口：它没有统一的 `inject/verify/cleanup` 子命令，没有独立的 cleanup actor 记录，也没有把真值与调查模型隔离。2026-11-04T09:30Z 从仓库路径执行过一次，以 0 退出，结果见运行记录第 4 节。

## 6. 计时与结果

以下为四次有效运行的实测值。运行 2 是脚本缺陷导致的无效尝试，已保留在运行记录中，不计入通过次数。

| 阶段 | 运行 1（手工） | 运行 3（脚本） | 运行 4（仓库脚本） | 运行 5（中性镜像） |
|---|---|---|---|---|
| 注入到新 Pod Ready | 未单独记录 | 2 秒 | 3 秒 | 2 秒 |
| 注入到首页 500 | 未记录首次时刻 | 22 秒 | 3 秒 | 22 秒 |
| 注入到告警 firing | 约 1 分钟 | 87 秒 | 72 秒 | 78 秒 |
| 回退耗时 | 未单独记录 | 1 秒 | 1 秒 | 2 秒 |
| 回退到探测首次恢复 | 约 5 秒 | 26 秒 | 26 秒 | 31 秒 |
| 回退到告警清零 | 约 10 秒 | 88 秒 | 87 秒 | 92 秒 |
| 注入到告警清零总耗时 | 未测 | 177 秒 | 162 秒 | 173 秒 |

运行 3、运行 4 和运行 5 使用同一规则和同一脚本，运行 5 换成中性镜像；三者的首页 500 与告警 firing 时刻仍不相同，说明耗时受探测周期、调度和端口转发就绪时间影响，不能把单次值当作固定值。运行 1 的恢复计时起点与脚本运行不同，不能直接比较。

运行 1 的 loadgenerator 在注入前为 359 次请求、0 失败；运行 3 结束时累计 8666 次请求、189 失败；运行 4 结束时累计 13978 次请求、225 失败；运行 5 结束时累计 9290 次请求、187 失败。失败集中在各次故障窗口内，累计比例随时间稀释，不能当作稳态错误率。

## 7. 未完成

- 没有统一的 `inject/verify/cleanup` 命令和场景参数文件，`repeat.sh` 中的镜像、工作负载和规则仍为固定值。
- 没有把真值、脚本和命名从调查模型可读范围中隔离。镜像标签和错误文本已中性化，但工具层没有访问控制。
- 没有独立 cleanup actor 记录；回退即是复位，尚未验证“Agent 已恢复后 cleanup 只做 no-op”的分支。
- 没有逐步截图和原始查询导出。
- 只验证了单一工作负载和单一注入点，没有覆盖镜像拉取失败、旧 Pod 继续服务等反例。
