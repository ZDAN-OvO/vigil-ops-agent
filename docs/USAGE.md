# 操作手册：怎样用起来

配置 Vigil、发起调查、审批动作，并核查结果。

## 1. 页面调查

先执行 `uv run --frozen aiops dev db migrate`、`make web-build` 和
`uv run --frozen aiops doctor`，然后运行 `uv run --frozen aiops serve`。
打开终端显示的本机地址，默认是 `http://127.0.0.1:8080/`。

1. 选择受影响的服务，描述故障。首页故障选择 `frontend`。
2. 在对话中补充线索，查看调查进展和证据。线索显示「已送入调查」时会标出调查轮次。
3. 核对回滚版本、Pod 模板变更、风险和关联证据，再批准或拒绝。
4. 查看集群写入状态与首页探针结果。拒绝后，本次调查继续取证。
5. 调查结束后可开始新调查。刷新或重新打开页面会恢复当前会话。

出现「处置结果待核实」时，到实验集群核实，再执行
`aiops dev followup resolve <待办 ID> --note "<核实结论>"`。记录会显示在页面。
服务运行期间，告警只取证。命令行演练和审批需要停止服务，页面会话的审批始终在页面完成。

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

宿主机端口被别的项目占用时先看 `ss -ltnp | grep <端口>`：本机 8081 被另一个前端项目占用，因此 webhook 用 8091、示例商店的 port-forward 用 8082，Vigil 页面用 8080。

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
VIGIL_WEB_HOST=127.0.0.1
VIGIL_WEB_PORT=8080
VIGIL_WEB_OPERATOR=web-operator
VIGIL_ACTION_APPROVAL_TTL_SECONDS=900
VIGIL_VERIFY_PROBE_QUERY=probe_http_status_code{job="frontend-probe"}
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
uv run --frozen aiops dev db status   # 期望列出 0001_init … 0013_web_conversation
uv run --frozen aiops dev db migrate  # 幂等；升级后先跑这个
```

## 4. 命令手册

`aiops --help` 列出服务与自检，`aiops dev --help` 列出开发命令。

| 命令 | 作用 | 写集群 |
|---|---|---|
| `aiops dev status` | 版本与能力范围，不探测集群 | 否 |
| `aiops dev lab scenarios` | 列出对照评估的场景与判定口径 | 否 |
| `aiops dev lab run` | 按场景跑对照实验并写记录 | 是（通过场景脚本） |
| `aiops dev lab summarize` / `lab rescore` | 汇总已有记录 / 按当前口径重算 | 否 |
| `aiops dev db status` / `db migrate` | 迁移状态 / 应用缺失迁移 | 否 |
| `aiops dev investigate --service --symptoms [--arm]` | 跑一次调查；`--arm` 选对照臂（默认 `agent`） | 仅自动允许的实验命名空间 `restart_pod` |
| `aiops dev incidents [--limit N]` | 最近事件与关联调查 | 否 |
| `aiops dev report <inv-id>` | 已存结论、假设、Skill、委派与观测条数 | 否 |
| `aiops dev act --kind rollout-undo --deployment X --reason "..."` | 提交回退提案 | 自动允许时当场执行 |
| `aiops dev act --kind restart-pod --pod P --service S --reason "..."` | 提交重建提案 | 实验命名空间自动执行 |
| `aiops dev approvals [--status]` | 待审批提案列表 | 否 |
| `aiops dev proposal <act-id>` | 单个提案 + 审批 + 执行 + 审计 + 待办 | 否 |
| `aiops dev approve <act-id> [--defer]` | 批准并执行；`--defer` 只记录审批 | 是 |
| `aiops dev resume <act-id>` | 执行已批准未执行的提案 | 是 |
| `aiops dev reject <act-id>` | 拒绝，不写集群 | 否 |
| `aiops dev cancel <inv-id>` | 请求取消运行中的单 Agent 任务；等待审批时直接终止 | 否 |
| `aiops dev followups` / `aiops dev followup <fol-id>` | 异常执行留下的人工待办 | 否 |
| `aiops dev knowledge list` / `knowledge import --path <文件或目录>` | 结构化 runbook 查询与登记 | 否 |
| `aiops serve` | 启动本机页面与独立告警接入 | 页面批准后可执行回滚 |
| `aiops doctor` | 检查数据库、模型网关、集群、Prometheus、业务探针、准入规则与页面文件 | 否 |

除 `serve`、`doctor`、`knowledge` 外，表中命令支持 `--output json`，便于脚本消费。

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

记录人工核实结果用 `aiops dev followup resolve <fol-id> --note <核实说明>`。

`serve` 提示存在命令行来源的未决执行而拒绝启动时，先运行 `aiops dev adopt-pending` 把它们转成待办，到集群核实后再 `followup resolve`。

## 5. 命令行演练

环境就绪后，手动发起调查并读取报告：

```bash
uv run --frozen aiops dev investigate --service frontend --symptoms "首页 500"
uv run --frozen aiops dev report <inv-id>
```

告警触发时先启动 `aiops serve`。收到 webhook 后返回 202；同一指纹重复投递返回 `deduplicated`。用 `aiops dev incidents --limit 3` 找到事件与调查 ID。

报告列出假设、观测与缺失证据。等待审批时，核对提案的目标、参数和审计记录，再决定批准或拒绝：

```bash
uv run --frozen aiops dev approvals
uv run --frozen aiops dev proposal <act-id>
uv run --frozen aiops dev approve <act-id>
```

拒绝提案使用 `aiops dev reject <act-id>`。没有现成提案时，操作者可以提交回退请求：

```bash
uv run --frozen aiops dev act --kind rollout-undo \
  --deployment <deployment> --reason "回退原因" --investigation <inv-id>
```

审批有效期由 `VIGIL_ACTION_APPROVAL_TTL_SECONDS` 控制，默认 900 秒。超时、目标 resourceVersion 变化或提案哈希不一致都会拒绝执行，需要重新提案。执行后核查提案中的验证结果；有人工待办时，用 `aiops dev followups` 查看原因。

故障注入与复位使用[场景操作步骤](../faults/README.md)，操作者复位与 Agent 恢复分别记录。

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
| 执行后出现 followup 而非 verified | 动作没改善或样本不足 | `aiops dev followups` 看原因，人工核实 |

## 7. 数据在哪、怎么清

- 业务数据：PostgreSQL `vigil` 库。事件/调查/假设/观测/工具账本/委派/提案/审批/执行/审计/待办/runbook 各一张表，迁移文件在 `src/aiops/migrations/`。
- 运行产物：`runtime/`（`VIGIL_RUNTIME_DIR`），存放大观测的原始文件，不入库。它是临时工作目录，不是证据存档。
- 验收记录：`faults/<场景>/runs/` 保存结论，`.evidence/` 保存不提交的本地截图。
- 历史遗留：一次演练会在库里留若干 `pending_approval` 提案与 `open` 待办，属正常记录；旧提案过期后再批准会因 `approval_expired` 拒绝。需要干净基线时可以重建库。

  > **警告**：下面的命令会删除 `vigil` 库中的全部业务数据和 checkpoint，无法恢复。只在确认不再需要现有记录时执行。

  ```bash
  docker exec vigil-postgres psql -U vigil -d vigil -c 'drop schema public cascade; create schema public;'
  uv run --frozen aiops dev db migrate
  ```

## 8. 自检

```bash
uv run --frozen aiops dev db status
uv run --frozen aiops dev investigate --service frontend --symptoms "冒烟"
uv run --frozen aiops dev report <上一步返回的 inv-id>
uv run --frozen python scripts/skill_runtime_gate.py
```

`skill_runtime_gate.py` 检查网关 tool calling、结构化终态、checkpoint、Skill 信号匹配与只读子 Agent；失败项会写明原因，不要在未通过时宣称接入完成。

## 9. 对照评估

对照评估比较三条臂在同故障集上的表现，口径与门槛写在
[ADR-0013](adr/0013-comparative-evaluation.md)。三条臂共用同一套工具、动作规则和记录表，
差别只在判定方式：

| 臂 | 说明 |
|---|---|
| `agent` | 现行运行时：`create_agent` + Skill 目录 + 排查方法提示词 |
| `generic` | 同一个循环，去掉 Skill 与排查方法提示词 |
| `rules` | 固定规则链，不调用模型 |

### 先看场景与口径

```bash
uv run --frozen aiops dev lab scenarios
```

它会打印每个场景的真值类别、期望动作和证据检查项。**场景真值只给评估者看**，
调查侧读不到；`faults/` 目录也不在工具可读范围内。

### 跑一批对照

```bash
# 单场景单臂，先确认链路通
uv run --frozen aiops dev lab run --scenario a01-pod-rebuild --arm rules --runs 1

# 正式批次：全部场景、三条臂、每格 3 次
uv run --frozen aiops dev lab run --runs 3
```

默认记录写到 `faults/runs/<日期>-comparison.json`，同时生成同名 `.md` 汇总。
`--record` 可以指定路径；`.evidence/` 下的记录不提交。

一次运行的顺序固定：`preflight → inject → verify → 调查臂 → recovered → cleanup`。
`preflight`、`inject`、`verify` 任一步失败都判该次无效并写明原因，臂本身不运行，环境照样复位。
`a01` 的 `preflight` 会先部署健康版本，因此脚本用 `RESULT ... created=0|1` 自报这次是否
真的建过资源；只有自报创建过才会在失败后复位，避免把别人的对象一起删掉。故障已成立、臂已
跑过之后若收尾采集出错，该条仍算有效样本并标 `collection_error`，不会被剔除出分母。
**实验会改集群**：`f01`/`f02` 作用于 `vigil-shop` 的既有工作负载，`a01` 在
`vigil-experiment` 里自建命名空间。不要在非实验集群上跑。

### 读记录与重算

```bash
uv run --frozen aiops dev lab summarize --record faults/runs/2026-11-18-comparison.json
uv run --frozen aiops dev lab rescore --record faults/runs/2026-11-18-comparison.json
```

`lab rescore` 只按当前判定口径重算已有记录，不重跑实验，也不改实验事实。改了词表或门槛
必须重算，不允许只在新实验上生效。

重算只用记录里的 `facts.snapshot`（结论、假设、支持观测、提案、尝试、成本的完整快照），
不回头读数据库；实验之后的审批或清理都不会改写历史结论。老记录没有快照时，首次重算会补
采一份并标 `snapshot_source=db` 与 `snapshot_captured_at`。

判读规则（与 ADR-0013 一致，跑之前就定死）：

- 每格至少 3 次有效运行才计入比较；
- 诊断正确率不低于 2/3 且越界写操作为 0，才算该臂在该场景达标；
- 一个臂要"更好且没有任何维度更差"才算相对收益，否则明确写"不构成对照结论"。

`needs_review` 表示结论措辞没命中词表、但证据与动作都对：不要直接判错，回运行记录里
人工核对结论原文。
