# F03 Redis 访问链路延迟

状态：2026-11-04 在 `vigil-lab` 完成一次完整实验。实验代理把 `redis-cart` 的访问路径切到带固定延迟的 TCP 转发层，业务延迟从约 10ms 升到约 1-1.5s，探针仍成功；调查定位到调用链变化、未声称 Redis 服务端内部慢查询；判定后由操作者清理并回到基线。证据见 [`demos/ms04/`](../../demos/ms04/README.md)。

## 1. 场景与真值

场景卡见 [MVP F03](../../docs/mvp.md#f03redis-访问链路延迟)。

- 载体：`cartservice` → `redis-cart:6379` 的访问路径。
- 注入：新增实验 TCP 代理 `redis-access-proxy`，每块转发延迟 500ms（一次请求往返约 1s）；新增 `redis-cart-direct` 指向真实 Redis Pod，把 `redis-cart` Service 的 selector 改到代理。
- 预期：只命中 Redis 的请求（首页购物车、/cart、/product）变慢，探针仍成功、错误率不升；Redis Pod 本身健康、无重启、资源规格不变。
- 反向边界：不能把代理延迟归因于 Redis 服务端慢查询；没有服务端指标时只能说明调用链变化与时间相关性。现有动作不能移除 Service 路由或代理，正确结果是停止并交人工处理。
- 选择理由：`cartservice` 的 Deployment 模板不变（revision 1），故障不在 Deployment 历史里，`rollout_undo` 无法修复；代理和 Service 变更不进入业务 Deployment 模板，符合“现有动作不适用”。

真值只保存在本目录和运行证据中。调查模型能看到的是集群对象、Service 选择器、Pod 状态、指标和日志；本目录内容不进模型上下文。

## 2. 注入机制

1. 构建并导入实验镜像 `vigil/redis-access-proxy:20261105`（本地构建，不推送外部仓库）。
2. 应用 [`manifests.yaml`](manifests.yaml)：`redis-cart-direct` Service 指向真实 Redis，`redis-access-proxy` Deployment 转发到 `redis-cart-direct:6379`。
3. 把 `redis-cart` Service 的 selector 从 `app=redis-cart` 改为 `app=redis-access-proxy`。
4. 删除 `cartservice` Pod，让它新建的 Redis 连接经过代理。

第 4 步是必须的实验步骤：既有 Redis 长连接不会因为 Service selector 变化而迁移。第一次注入后没有重启 `cartservice`，探针仍约 10ms，验证失败；删除 Pod 后延迟才出现。该失败记录保留。

注入只改 Service 和新增实验对象，不改 `cartservice`/`redis-cart` 的 Deployment 模板；`cartservice` 的 Pod 重建不产生新 revision。业务 Pod 重建由注入器执行，不是 Agent 动作。

## 3. 冻结参数

| 项目 | 取值 | 依据 |
|---|---|---|
| 代理延迟 | 每块 500ms，往返约 1s | 足以超过 300ms 判定阈值，又低于探针 5s 超时 |
| 业务信号 | `avg_over_time(probe_duration_seconds{job="frontend-probe"}[2m])` | 独立真实 HTTP 探针 |
| 延迟判定 | 2 分钟均值 ≥ 300ms 且 `probe_success=1` | 区分变慢与不可用 |
| 附加信号 | 前端 `/cart` 日志 `took_ms`、cartservice 调用日志 | 依赖链路证据 |
| 清理判定 | 2 分钟均值 < 100ms、`probe_success=1`、`/cart` 200 | 回到注入前量级 |
| 观察窗 | 验证与清理各最多 300 秒 | 覆盖 2 分钟滑动均值 |

500ms 是实验值，不是通用阈值；真实环境的代理延迟由场景决定。

## 4. 操作步骤

```bash
docker build -t vigil/redis-access-proxy:20261105 faults/f03-redis-latency
k3d image import vigil/redis-access-proxy:20261105 -c vigil-lab
bash faults/f03-redis-latency/repeat.sh inject
bash faults/f03-redis-latency/repeat.sh verify
# 调查判定后
bash faults/f03-redis-latency/repeat.sh cleanup
```

脚本依赖 `kubectl`、`curl`、`python3` 和 8080 端口的前端 port-forward。`cleanup` 是操作者清理，记录在证据里，不计 Agent 恢复。

## 5. 运行结果

| 阶段 | 时间（UTC） | 结果 |
|---|---|---|
| 基线 | 17:36:45 | 探针 2 分钟均值 12ms，首页 29ms，/cart 6ms，Redis/cartservice 0 重启 |
| 注入 | 17:36:55 | selector 改为 `app=redis-access-proxy`，endpoints 指向代理 |
| 第一次验证 | 17:37:12–17:42:17 | 失败：探针仍约 10ms，既有 Redis 长连接绕过代理 |
| 删除 cartservice Pod | 17:42:30 | 新连接经过代理 |
| 第二次验证 | 17:43:36 | 探针 2 分钟均值 1036ms，首页 1.24s，/cart 1.5s，`probe_success=1` |
| 调查 | 17:47:25–17:57:53 | `completed`，16 条证据、16 次工具调用、6 次模型调用；定位 Service selector 与代理层，未声称 Redis 服务端原因 |
| 清理 | 17:58:17–18:00:53 | selector 恢复，代理与 direct Service 删除，探针回到 10ms |
| 最终状态 | 18:01:09 | 首页 8.5ms，/cart 90ms（单次含慢请求），探针 1，告警 0，0 重启 |

前端日志在故障窗口内显示 `/cart` POST `took_ms` 2005–2507、GET 1212–1507，而 `/_healthz` 为 0ms；cartservice 日志只有 `GetCartAsync`/`AddItemAsync` 调用记录，没有单次耗时。

## 6. 未完成

- 没有代理逐跳耗时或 Redis 服务端延迟指标；调查只能确认调用链被插入代理和时间吻合，不能量化代理的确切开销。`cartservice` 日志不含 Redis 调用耗时。
- 没有延迟类告警规则；调查由操作者观察后发起，不是告警自动触发。
- 增加 `list_services` 和 `list_logs` 两个只读工具后，第二次调查用 16 次工具调用、约 10 分 28 秒完成，超过 480 秒的墙钟预算；预算只在两次模型调用之间检查，无法中断进行中的调用。
- 代理镜像只在本地构建和导入，未推送外部仓库；只有单副本、单节点。
- 既有 Redis 长连接不随 Service selector 迁移，需要重启依赖 Pod 才能让故障生效；这是实验机制的一部分，不能当作 Agent 处置。
