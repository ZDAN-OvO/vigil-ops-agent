# MS04 证据报告：Redis 访问链路延迟调查

状态：2026-11-04 在 `vigil-lab` 完成一次真实运行。实验代理让 Redis 访问路径增加约 1s 延迟，调查定位到 Service 选择器与代理层变化，说明证据边界并拒绝不适用动作；判定后由操作者清理，业务回到基线。命令输出与集群观测在 [`outputs/`](outputs/)。

## 1. 结论

- 延迟只出现在命中 Redis 的路径：首页约 1.0-1.5s、`/cart` 约 1.0-1.5s、`/product` 约 1.2-1.5s，而 `/_healthz` 仍为 0ms；`probe_success` 保持 1，错误率未升。
- 调查定位到 `redis-cart` Service 的 selector 从 `app=redis-cart` 改为 `app=redis-access-proxy`，原后端保留在 `redis-cart-direct`；代理 Pod 健康、Redis Pod 无重启、`cartservice` Deployment revision 仍为 1。
- 调查没有把延迟归因于 Redis 服务端内部慢查询，并明确说明缺少代理逐跳耗时和 Redis 服务端指标；给出的人工待办是评估代理层与 Service 路由变更。
- 现有动作无法移除 Service 路由或代理，调查 `action_kind=none`，数据库没有提案，审计只有调查开始与完成。清理由操作者执行，清理后的健康不计 Agent 恢复。

## 2. 环境与版本

| 项目 | 值 |
|---|---|
| 集群 | `vigil-lab`，k3s v1.35.5+k3s1，单节点 |
| 业务 | Online Boutique v0.9.0，12 个 Deployment |
| 路径 | `cartservice` → `redis-cart:6379` → 真实 Redis Pod |
| 代理 | `vigil/redis-access-proxy:20261105`，本地构建并 `k3d image import` |
| 延迟 | 每块 500ms，一次 PING 实测 1004ms |
| 调查预算 | 16 次工具调用、8 次模型调用、480 秒墙钟 |

## 3. 时间线

所有时间均为 UTC。

| 时间 | 阶段 | 证据 |
|---|---|---|
| 17:36:45 | 基线：探针 2 分钟均值 12ms、首页 29ms、/cart 6ms、0 重启 | [`01-baseline.txt`](outputs/01-baseline.txt) |
| 17:36:55 | 注入：代理就绪，`redis-cart` selector 改为 `app=redis-access-proxy` | [`02-inject.txt`](outputs/02-inject.txt) |
| 17:37:12–17:42:17 | 第一次验证失败：探针仍约 10ms，既有 Redis 长连接绕过代理 | [`03a-verify-first-attempt.txt`](outputs/03a-verify-first-attempt.txt) |
| 17:42:30 | 注入器删除 `cartservice` Pod，让新连接经过代理 | 时间线记录 |
| 17:43:36 | 第二次验证通过：探针 1036ms、首页 1.24s、/cart 1.5s、`probe_success=1` | [`03-verify.txt`](outputs/03-verify.txt) |
| 17:43:49 | 平台证据：selector、endpoints、0 重启、`cartservice` revision 1、前端 `took_ms` 2005 | [`04-platform.txt`](outputs/04-platform.txt) |
| 17:44:02–17:46:58 | 第一次调查 `inv-cb813652ade4` 预算耗尽，12 次工具调用后 aborted | [`05a-investigation-budget.txt`](outputs/05a-investigation-budget.txt) |
| 17:47:25–17:57:53 | 第二次调查 `inv-36e5c873f9ce` completed：16 条证据、16 次工具调用、6 次模型调用 | [`05-investigation.txt`](outputs/05-investigation.txt)、[`05b-investigation-report.txt`](outputs/05b-investigation-report.txt) |
| 17:58:17–18:00:53 | 操作者清理：恢复 selector，删除代理与 direct Service，探针回到 10ms | [`06-cleanup.txt`](outputs/06-cleanup.txt) |
| 18:01:09 | 最终状态：首页 8.5ms、探针 1、告警 0、0 重启 | [`07-final.txt`](outputs/07-final.txt) |

## 4. 分步说明

1. **两类互补信号。** 业务侧是独立黑盒 HTTP 探针的 `probe_duration_seconds` 从 12ms 升到约 1.0s，以及前端 `/cart`、`/product` 日志的 `took_ms`；依赖侧是 `cartservice` 日志中的 `GetCartAsync`/`AddItemAsync` 调用记录和 Service/Endpoint 路由。`cartservice` 日志没有单次 Redis 耗时，报告如实标注这一缺口。
2. **定位调用链。** 调查列出全部 Pod/Deployment/Service：`frontend` 健康、0 重启、revision 1；`redis-cart` 的 selector 指向新增的 `redis-access-proxy`，原后端在 `redis-cart-direct`；代理和 Redis Pod 均 0 重启；`cartservice` Deployment revision 1、Pod 于 17:42:30 重建后延迟跃升。结论把变化定位到 Redis 访问路径的代理层，而不是 frontend 或 Redis 进程。
3. **证据边界。** 结论原文说明代理日志为空、没有逐跳耗时或 Redis 侧延迟指标、`cartservice` 日志没有耗时时长，因此只能确认调用链被插入一层代理且时间点吻合，不能量化代理开销；没有声称 Redis 服务端慢查询。
4. **停止与不适用判断。** 故障不在任何 Deployment 的 revision 里，`rollout_undo` 无法修复；调查 `action_kind=none`，数据库无提案，审计只有 `investigation.started` 与 `investigation.completed`。没有把 Pod 重建或重启当作修复。
5. **清理归属。** 调查判定后，操作者执行 `cleanup`：恢复 `redis-cart` selector、删除代理 Deployment 与 `redis-cart-direct`，探针回到 10ms、首页 200。清理后的健康单独记录，不计 Agent 恢复。
6. **失败保留。** 第一次注入后的验证失败，原因是既有 Redis 长连接不随 Service selector 迁移；第一次调查因 12 次工具预算耗尽而 aborted。两次失败都保留在证据里，不计通过次数。

## 5. 可查记录

- 第一次调查：`inv-cb813652ade4`，aborted，`预算耗尽：tool_calls`。
- 第二次调查：`inv-36e5c873f9ce`，completed，16 条证据，16 次工具调用，6 次模型调用。
- 提案：无。`action_proposals` 中该调查的记录数为 0；审计只有开始与完成。
- 清理：操作者执行，17:58:17–18:00:53；最终 `redis-cart` selector 为 `app=redis-cart`，代理 Deployment 不存在。

## 6. 失败与缺口

1. 既有 Redis 长连接不随 Service selector 迁移，第一次验证完全无效；注入脚本已补上重启 `cartservice` Pod 的步骤，但这是实验机制，不是 Agent 动作。
2. 第一次调查用满 12 次工具调用仍未结束，预算提高后才完成。第二次调查实际耗时 10 分 28 秒，超过 480 秒墙钟预算；预算只在模型调用之间检查，不能中断进行中的调用。
3. 没有延迟类告警规则，调查由操作者观察后发起；自动告警入口没有覆盖本场景。
4. 代理没有逐跳耗时指标，Redis 服务端也没有延迟指标；`cartservice` 日志不含每次 Redis 调用的耗时，调查只能做到调用链定位和时间相关性。
5. 代理镜像只在本地构建和导入，未推送外部仓库；单副本、单节点。
6. 清理耗时约 2.5 分钟，主要是 2 分钟滑动均值回落；期间前端仍可访问，只是逐渐恢复。
7. 调查结论提到 `paymentservice` 的 17:14:22 单次 OOMKilled 与 `currencyservice` 的历史回滚，作为无关背景排除；它们属于此前 MS03 运行留下的观测。

## 7. 复现

前置：集群健康，前端与 Prometheus 的 port-forward 在位，`.env` 含数据库、模型和 kubeconfig，代理镜像已导入。

```bash
bash faults/f03-redis-latency/repeat.sh inject
bash faults/f03-redis-latency/repeat.sh verify
uv run --frozen aiops investigate --service frontend --symptoms "首页和购物车请求延迟从约 10ms 上升到约 1-2s，probe_success 仍为 1，没有业务错误" --lookback-minutes 15
bash faults/f03-redis-latency/repeat.sh cleanup
```

## 8. 复位

最终 `redis-cart` selector 为 `app=redis-cart`，`redis-access-proxy` Deployment 与 `redis-cart-direct` Service 已删除；首页 8.5ms、探针 1、告警 0、Redis/cartservice/frontend 0 重启。port-forward 由操作者停止；没有删除业务工作负载。
