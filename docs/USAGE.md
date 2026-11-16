# 操作手册：怎样用起来

本文只讲怎么操作、看什么、失败了怎么办。全局结构见[架构](../ARCHITECTURE.md)，实验环境的搭建见[实验环境](../env/README.md)。

## 1. 这套东西是什么

三条链路，日常用法各不同：

| 链路 | 入口 | 谁触发 | 产物 |
|---|---|---|---|
| 告警 → 调查 | `aiops serve` + AlertManager | Prometheus 真实告警 | 事件、调查、提案；满足自动规则时可执行 Pod 重建 |
| 手动调查 | `aiops investigate` | 你自己 | 事件、调查、Skill 与委派记录、提案 |
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

### 升级限制

在 0010 版本上做过的调查，其旧 `evidence` 表记录会在升级到 `0011_skills_and_delegations` 时被丢弃，不会转成 `observations`；需要保留时，升级前先导出，旧调查与旧报告不保证可读。

迁移同时删除旧 `hypothesis_revisions` 与 `tool_calls` 表，见 [0011 迁移](../src/aiops/migrations/0011_skills_and_delegations.sql)。迁移前的导出应覆盖需要保留的旧记录。

```bash
uv run --frozen aiops db status   # 期望列出 0001_init … 0011_skills_and_delegations
uv run --frozen aiops db migrate  # 幂等；升级后先跑这个
```

## 4. 命令手册

| 命令 | 作用 | 写集群 |
|---|---|---|
| `aiops status` | 版本与能力范围，不探测集群 | 否 |
| `aiops db status` / `db migrate` | 迁移状态 / 应用缺失迁移 | 否 |
| `aiops investigate --service --symptoms` | 跑一次调查；单 Agent 可能等待审批（退出 3） | 仅自动允许的实验命名空间 `restart_pod` |
| `aiops incidents [--limit N]` | 最近事件与关联调查 | 否 |
| `aiops report <inv-id>` | 已存结论、假设、Skill、委派与观测条数 | 否 |
| `aiops act --kind rollout-undo --deployment X --reason "..."` | 提交回退提案 | 自动允许时当场执行 |
| `aiops act --kind restart-pod --pod P --service S --reason "..."` | 提交重建提案 | 实验命名空间自动执行 |
| `aiops approvals [--status]` | 待审批提案列表 | 否 |
| `aiops proposal <act-id>` | 单个提案 + 审批 + 执行 + 审计 + 待办 | 否 |
| `aiops approve <act-id> [--defer]` | 批准并执行；`--defer` 只记录审批 | 是 |
| `aiops resume <act-id>` | 执行已批准未执行的提案 | 是 |
| `aiops reject <act-id>` | 拒绝，不写集群 | 否 |
| `aiops cancel <inv-id>` | 请求取消运行中的单 Agent 任务；等待审批时直接终止 | 否 |
| `aiops followups` / `followup <fol-id>` | 异常执行留下的人工待办 | 否 |
| `aiops knowledge list` / `knowledge import --path <文件或目录>` | 结构化 runbook 查询与登记 | 否 |
| `aiops serve` | 启动 AlertManager webhook | 否 |

除 `serve`、`knowledge` 外，表中命令支持 `--output json`，便于脚本消费。

### 退出码

退出码 0 的异步 `accepted` 只表示请求已接收，不代表调查或修复成功；任务仍需通过报告核实。

| 码 | `investigate`（单 Agent） | `approve` / `resume` / `act` |
|---|---|---|
| 0 | 任务 recovered 或仅诊断完成 | 执行且验证通过 |
| 1 | 运行失败、预算耗尽等 | 已执行但未验证通过 / 存储或参数错误 |
| 2 | 参数／用法错误 | 参数／用法错误 |
| 3 | 等待审批，或 report 有需人工跟进项 | 提案已创建，等待审批（没有执行） |
| 4 | 策略拒绝 | 策略拒绝，未执行 |
| 5 | 信息不足，未形成根因候选 | — |

调查任务状态看 `report` 与 `data.task`（phase/outcome/termination_code）；`investigations.status` 只表示诊断结论。`data.skills` 记录本轮加载的 Skill 与内容哈希，`data.delegations` 记录只读委派，每条包含委派 ID、子任务 Skill 和 `observation_ids`（该委派返回的观测）。

## 5. 一条完整演练（坏发布，约 6 分钟）

所有写操作都限定在 `vigil-shop`，坏发布的载体是 `productcatalogservice`，回退即复位。场景细节见[坏发布场景](../faults/f01-bad-release/README.md)。

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

4. **看调查结果**（预算 28 次工具调用 / 20 次模型调用 / 600 秒墙钟，通常 2–4 分钟；依据见 [ADR-0011](adr/0011-investigation-budget.md)）

   ```bash
   uv run --frozen aiops report inv-xxxxxxxx
   ```

   预算耗尽时状态是 `inconclusive` 并给终止原因，这不算失败；报告会列出假设、证据边界与缺失证据。

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

- 业务数据：PostgreSQL `vigil` 库。事件/调查/假设/观测/工具账本/委派/提案/审批/执行/审计/待办/runbook 各一张表，迁移文件在 `src/aiops/migrations/`。
- 运行产物：`runtime/`（`VIGIL_RUNTIME_DIR`），存放大观测的原始文件，不入库。它是临时工作目录，不是证据存档。
- 验收证据：截图贴在 PR 评论里，运行结论写入 `faults/<场景>/runs/`。本地生成的截图放在 `.evidence/`，该目录不提交。
- 历史遗留：一次演练会在库里留若干 `pending_approval` 提案与 `open` 待办，属正常记录；旧提案过期后再批准会因 `approval_expired` 拒绝。需要干净基线时可以重建库。

  > **警告**：下面的命令会删除 `vigil` 库中的全部业务数据和 checkpoint，无法恢复。只在确认不再需要现有记录时执行。

  ```bash
  docker exec vigil-postgres psql -U vigil -d vigil -c 'drop schema public cascade; create schema public;'
  uv run --frozen aiops db migrate
  ```

## 8. 自检

```bash
uv run --frozen aiops db status
uv run --frozen aiops investigate --service frontend --symptoms "冒烟"
uv run --frozen aiops report <上一步返回的 inv-id>
uv run --frozen python scripts/skill_runtime_gate.py
```

`skill_runtime_gate.py` 检查网关 tool calling、结构化终态、checkpoint、Skill 信号匹配与只读子 Agent；失败项会写明原因，不要在未通过时宣称接入完成。
