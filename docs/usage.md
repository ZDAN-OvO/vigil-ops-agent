# 操作手册：怎样用起来

状态：2026-11-09 在本机实验环境逐条实跑验证（k3d `vigil-lab`、`vigil-postgres`、网关 `127.0.0.1:3425`）。本文只写当前代码真实支持的操作和退出码；没有代码支撑的步骤不写进来。

产品边界与验收标准见 [MVP](mvp.md) 与[里程碑](milestone.md)，本文不讲设计理由，只讲怎么操作、看什么、失败怎么办。

## 1. 这套东西是什么

三条链路，日常用法各不同：

| 链路 | 入口 | 谁触发 | 产物 |
|---|---|---|---|
| 告警 → 调查 | `aiops serve` + AlertManager | Prometheus 真实告警 | 事件、调查、提案（不执行写操作） |
| 手动调查 | `aiops investigate` | 你自己 | 同上，可指定 `--mode rules` 走零模型基线 |
| 受控处置 | `aiops act` / `approve` / `resume` | 操作者提交与审批 | 提案、审批、执行尝试、验证结果、审计 |

调查默认只读；单 Agent 在 `vigil-experiment` 内可对满足规则的 `restart_pod` 自动执行，其余动作等人工审批。操作者入口 `act`/`approve`/`resume` 写集群，动作只有 `rollout_undo`（回退到上一 revision）和 `restart_pod`（重建 Pod）两种。实验命名空间之外默认拒绝。

## 2. 前置：先确认环境活着

四样东西缺一不可。本机现状与检查命令：

```bash
# 1) 实验集群
k3d cluster list                      # 期望 vigil-lab 1/1
export KUBECONFIG=$HOME/.kube/vigil-lab.yaml
kubectl -n vigil-shop get deploy      # 期望 12 个工作负载 1/1

# 2) 观测栈（告警来源）
kubectl -n vigil-monitoring get pods  # prometheus / alertmanager / blackbox / ksm

# 3) 实验 PostgreSQL
docker ps --filter name=vigil-postgres

# 4) 模型网关（仅 agent 模式需要）
curl -s http://127.0.0.1:3425/v1/models | head -c 200
```

`kubectl` 默认不读 `~/.kube/vigil-lab.yaml`，但 `aiops` 通过 `VIGIL_KUBECONFIG` 读它，两者互不影响。`repeat.sh` 等脚本会自己 export，不必依赖 shell 环境。

宿主机端口被别的项目占用时先看 `ss -ltnp | grep <端口>`：本机 8081 被另一个前端项目占用，因此 webhook 用 8091、前端 port-forward 用 8082。

### 需要时启动的服务

```bash
# Prometheus / AlertManager / 前端，各自一个终端（或交给进程管理器）
kubectl -n vigil-monitoring port-forward svc/prometheus 9090:9090
kubectl -n vigil-monitoring port-forward svc/alertmanager 9093:9093
kubectl -n vigil-shop port-forward svc/frontend 8082:80
```

port-forward 随 Pod 重启会断，`aiops` 报 `Prometheus 不可用` 时先重连。

## 3. 配置：`.env`

仓库根目录 `.env`（`gitignore` 已忽略，`.env.example` 只有占位）。下面是本机实跑用的一份：

```ini
VIGIL_DATABASE_URL=postgresql://vigil:<password>@127.0.0.1:55432/vigil
VIGIL_KUBECONFIG=/home/zdan/.kube/vigil-lab.yaml
VIGIL_PROMETHEUS_URL=http://127.0.0.1:9090
VIGIL_CLUSTER=vigil-lab
VIGIL_NAMESPACE=vigil-shop

# OpenAI 兼容网关，模型 ID 必填；实际返回模型由网关决定
VIGIL_MODEL_BASE_URL=http://127.0.0.1:3425
VIGIL_MODEL_NAME=auto/deepseek-v4.1-flash

# webhook 鉴权与监听。token 与集群 secret 一致，否则 401
VIGIL_WEBHOOK_TOKEN=<与 vigil-monitoring/vigil-webhook-token 相同>
VIGIL_WEBHOOK_HOST=0.0.0.0   # 集群经 docker 网关 172.20.0.1 访问宿主机
VIGIL_WEBHOOK_PORT=8091
```

密码和 token 只放在 `.env`；需要重取时：

```bash
docker inspect vigil-postgres --format '{{range .Config.Env}}{{println .}}{{end}}' | grep POSTGRES_PASSWORD
kubectl -n vigil-monitoring get secret vigil-webhook-token -o jsonpath='{.data.token}' | base64 -d
```

连接检查：

```bash
uv run --frozen aiops db status   # 期望列出 0001_init … 0010_agent_task_and_ledger
uv run --frozen aiops db migrate  # 幂等；升级后先跑这个
```

## 4. 命令手册

| 命令 | 作用 | 写集群 |
|---|---|---|
| `aiops status` | 版本与能力范围，不探测集群 | 否 |
| `aiops db status` / `db migrate` | 迁移状态 / 应用缺失迁移 | 否 |
| `aiops investigate --service --symptoms [--mode rules]` | 跑一次调查；单 Agent 可能等待审批（退出 3） | 仅自动允许的实验命名空间 `restart_pod` |
| `aiops incidents [--limit N]` | 最近事件与关联调查 | 否 |
| `aiops report <inv-id>` | 已存结论、假设、证据条数 | 否 |
| `aiops explain <inv-id> "<问题>"` | 只读追问，只用已存记录回答 | 否 |
| `aiops act --kind rollout-undo --deployment X --reason "..."` | 提交回退提案 | 自动允许时当场执行 |
| `aiops act --kind restart-pod --pod P --service S --reason "..."` | 提交重建提案 | 实验命名空间自动执行 |
| `aiops approvals [--status]` | 待审批提案列表 | 否 |
| `aiops proposal <act-id>` | 单个提案 + 审批 + 执行 + 审计 + 待办 | 否 |
| `aiops approve <act-id> [--defer]` | 批准并执行；`--defer` 只记录审批 | 是 |
| `aiops resume <act-id>` | 执行已批准未执行的提案 | 是 |
| `aiops reject <act-id>` | 拒绝，不写集群 | 否 |
| `aiops cancel <inv-id>` | 请求取消运行中的单 Agent 任务；等待审批时直接终止 | 否 |
| `aiops followups` / `followup <fol-id>` | 异常执行留下的人工待办 | 否 |
| `aiops knowledge list` / `--import` | 结构化 runbook 查询与登记 | 否 |
| `aiops eval [--manifest-path]` | 按冻结清单核对 MVP 门槛 | 否 |
| `aiops serve` | 启动 AlertManager webhook | 否 |

所有命令支持 `--output json`（`serve` 除外），便于脚本消费。

### 退出码

| 码 | `investigate`（单 Agent） | `approve` / `resume` / `act` |
|---|---|---|
| 0 | 任务 recovered 或仅诊断完成 | 执行且验证通过 |
| 1 | 运行失败、预算耗尽等 | 已执行但未验证通过 / 存储或参数错误 |
| 2 | 参数／用法错误 | 参数／用法错误 |
| 3 | 等待审批，或 report 有需人工跟进项 | 提案已创建，等待审批（没有执行） |
| 4 | 策略拒绝 | 策略拒绝，未执行 |
| 5 | 信息不足，未形成根因候选 | — |

调查任务状态看 `report` 与 `data.task`（phase/outcome/termination_code）；`investigations.status` 只表示诊断结论。旧 `--mode rules` 调查仍返回旧语义。

## 5. 一条完整演练（F01 坏发布，约 6 分钟）

所有写操作都限定在 `vigil-shop`，坏发布的载体是 `productcatalogservice`，回退即复位。真实运行记录见 [demos/ms01](../demos/ms01/README.md)。

1. **基线**（终端 A/B 已启 port-forward 和 serve）

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8082/         # 200
   curl -s -G http://127.0.0.1:9090/api/v1/query \
     --data-urlencode 'query=probe_success{job="frontend-probe"}' | grep -o '"1"'  # 1
   ```

2. **注入**

   ```bash
   kubectl -n vigil-shop set image deployment/productcatalogservice \
     server=vigil/productcatalogservice:canary-20261104
   kubectl -n vigil-shop rollout status deploy/productcatalogservice --timeout=180s
   curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8082/         # 500
   ```

   注意容器名是 `server`，不是 `productcatalogservice`；写错会报 `unable to find container named`。

3. **等告警自动进链路**（`for: 1m` + `group_wait: 10s`，约 90 秒）

   ```bash
   curl -s http://127.0.0.1:9090/api/v1/alerts | grep -o '"state":"firing"'
   uv run --frozen aiops incidents --limit 3     # 出现 trigger=alert 的事件和 running 调查
   ```

   `aiops serve` 日志里应看到 `POST /webhook/alertmanager 202 Accepted`。同一指纹重复投递返回 `deduplicated`，不会重复调查。

4. **看调查结果**（预算 28 次工具调用 / 20 次模型调用 / 600 秒墙钟，通常 2–4 分钟；依据见 `docs/decisions.md` D26）

   ```bash
   uv run --frozen aiops report inv-xxxxxxxx
   uv run --frozen aiops explain inv-xxxxxxxx "为什么判断是这个服务"
   ```

   预算耗尽时状态是 `inconclusive` 并给终止原因，这不算失败，`explain` 会说明还缺哪条证据。

5. **提案与审批**

   ```bash
   uv run --frozen aiops approvals                      # 找目标 Deployment 的提案
   uv run --frozen aiops proposal act-xxxxxxxx          # 看清目标、revision、审计
   uv run --frozen aiops approve act-xxxxxxxx           # 退出 0 = 执行并验证通过
   ```

   没有现成提案时由操作者发起（模型结论不会自动变成写操作）：

   ```bash
   uv run --frozen aiops act --kind rollout-undo \
     --deployment productcatalogservice --reason "坏发布回退" \
     --investigation inv-xxxxxxxx
   ```

   审批有效期 `VIGIL_ACTION_APPROVAL_TTL_SECONDS`（默认 900 秒）。超时、目标 resourceVersion 变化、提案哈希不一致都会拒绝执行；需要重新提案。

6. **验证恢复并复位**

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8082/         # 200
   curl -s http://127.0.0.1:9090/api/v1/alerts                             # 活动告警清空
   kubectl -n vigil-shop get rs -l app=productcatalogservice               # 坏 RS 副本 0
   ```

   如果调查或审批没走完（比如预算耗尽、你拒绝了提案），手工复位：

   ```bash
   kubectl -n vigil-shop rollout undo deployment/productcatalogservice
   ```

   手工复位不能算作 Agent 恢复，报告里要分开写。

## 6. 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| `未配置 VIGIL_DATABASE_URL` | 缺 `.env` 或没在仓库根目录 | 检查根目录 `.env` |
| `StoreUnavailable` / 连接拒绝 | `vigil-postgres` 没起 | `docker start vigil-postgres` |
| Prometheus 相关报错 | port-forward 断了 | 重连 9090 |
| webhook `401 unauthorized` | token 与集群 secret 不一致 | 改成 secret 里的值并重启 `serve` |
| AlertManager 投递不到 | `serve` 只监听 127.0.0.1 / 端口不对 | `VIGIL_WEBHOOK_HOST=0.0.0.0`，配置里的 `172.20.0.1:<端口>` 对齐 |
| 模型调用失败 | 网关模型 ID 已下线 | `curl 127.0.0.1:3425/v1/models` 重新选别名 |
| `unable to find container named` | 用了 Deployment 名当容器名 | 先 `kubectl get deploy X -o jsonpath='{.spec.template.spec.containers[*].name}'` |
| `approval_expired` / `target_drift` | 审批过期或目标已变 | 重新 `act` 提案，不复用旧授权 |
| 执行后出现 followup 而非 verified | 动作没改善或样本不足 | `aiops followups` 看原因，人工核实 |

## 7. 数据在哪、怎么清

- 业务数据：PostgreSQL `vigil` 库。事件/调查/证据/工具调用/提案/审批/执行/审计/待办/runbook 各一张表，迁移文件在 `src/aiops/db.py`。
- 运行产物：`runtime/`（`VIGIL_RUNTIME_DIR`），不入库。`runtime/` 只是临时工作目录，不是证据存档；随手清掉后，`aiops eval` 的 V07 会因原始引用不存在而失败。
- 验收证据：需要长期回查的原始观测归档到 `demos/<里程碑>/runtime/`，清单的 `evidence_root` 指向那里。2026-11-09 复核就是因为旧证据只存在 `runtime/` 而失败，记录见 [demos/ms08](../demos/ms08/README.md#v07-复核与证据归档2026-11-09)。
- 演示证据：`demos/<里程碑>/`，截图与原始输出。
- 历史遗留：一次演练会在库里留若干 `pending_approval` 提案与 `open` 待办，属正常记录；旧提案过期后再批准会因 `approval_expired` 拒绝。需要干净基线时重建库：

  ```bash
  docker exec vigil-postgres psql -U vigil -d vigil -c 'drop schema public cascade; create schema public;'
  uv run --frozen aiops db migrate
  ```

## 8. 自检

```bash
uv run --frozen aiops db status
uv run --frozen aiops investigate --service frontend --symptoms "冒烟" --mode rules
uv run --frozen aiops report <上一步返回的 inv-id>
uv run --frozen aiops eval
```

`--mode rules` 不调模型、通常 2 秒内结束，适合验证数据库、Kubernetes 与 Prometheus 三条连接是否都通。`eval` 输出各门槛项通过/未通过；未通过项会写出具体缺失证据，不要在未通过时宣称完整 MVP。
