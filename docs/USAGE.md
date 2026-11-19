# 操作手册：怎样用起来

配置 Vigil、发起调查、审批动作，并核查结果。

## 1. 操作入口

三条链路，日常用法各不同。命令行入口正在重做，见设计文档；本节只写链路分工，操作步骤等新入口落地后补。

| 链路 | 入口 | 谁触发 | 产物 |
|---|---|---|---|
| 告警 → 调查 | AlertManager webhook | Prometheus 真实告警 | 事件、调查、提案；满足自动规则时可执行 Pod 重建 |
| 手动调查 | 对话式入口 | 你自己 | 事件、调查、Skill 与委派记录、提案 |
| 受控处置 | 对话式入口中的审批 | 操作者提交与审批 | 提案、审批、执行尝试、验证结果、审计 |

调查默认只读；`vigil-experiment` 内满足规则的 `restart_pod` 自动执行，其余动作等人工审批。动作只有 `rollout_undo`（回退到上一 revision）和 `restart_pod`（重建 Pod）两种。实验命名空间之外默认拒绝。

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

`kubectl` 默认不读 `~/.kube/vigil-lab.yaml`，但 Vigil 通过 `VIGIL_KUBECONFIG` 读它，两者互不影响。`repeat.sh` 等脚本会自己 export，不必依赖 shell 环境。

宿主机端口被别的项目占用时先看 `ss -ltnp | grep <端口>`：本机 8081 被另一个前端项目占用，因此 webhook 用 8091、前端 port-forward 用 8082。

### 需要时启动的服务

```bash
# Prometheus / AlertManager / 前端，各自一个终端（或交给进程管理器）
kubectl -n vigil-monitoring port-forward svc/prometheus 9090:9090
kubectl -n vigil-monitoring port-forward svc/alertmanager 9093:9093
kubectl -n vigil-shop port-forward svc/frontend 8082:80
```

port-forward 随 Pod 重启会断，Vigil 报 `Prometheus 不可用` 时先重连。

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

迁移原本靠旧 CLI 执行，入口随命令行一起重做，见设计文档；迁移 SQL 本身仍在 `src/aiops/migrations/` 并可重复执行。

## 4. 命令手册

命令行入口正在重做，见设计文档。本节原有的命令已随旧 CLI 删除，不再提供参数、退出码和 `--output json` 说明；入口实现后在此重写。

## 5. 调查与审批

环境就绪后由入口发起调查，再按报告核对流程：

1. 读报告里的假设、观测与缺失证据，确认结论是根因候选还是信息不足。
2. 等待审批时，核对提案的目标、参数和审计记录，再决定批准或拒绝。
3. 执行后核查提案里的验证结果；出现人工待办时，按待办原因人工核实。

审批有效期由 `VIGIL_ACTION_APPROVAL_TTL_SECONDS` 控制，默认 900 秒。超时、目标 resourceVersion 变化或提案哈希不一致都会拒绝执行，需要重新提案。

故障注入与复位使用[场景操作步骤](../faults/README.md)，操作者复位与 Agent 恢复分别记录。

## 6. 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| `未配置 VIGIL_DATABASE_URL` | 缺 `.env` 或没在仓库根目录 | 检查根目录 `.env` |
| `StoreUnavailable` / 连接拒绝 | `vigil-postgres` 没起 | `docker start vigil-postgres` |
| Prometheus 相关报错 | port-forward 断了 | 重连 9090 |
| webhook `401 unauthorized` | token 与集群 secret 不一致 | 改成 secret 里的值并重启接收端 |
| AlertManager 投递不到 | 接收端只监听 127.0.0.1 / 端口不对 | `VIGIL_WEBHOOK_HOST=0.0.0.0`，配置里的 `172.20.0.1:<端口>` 对齐 |
| 模型调用失败 | 网关模型 ID 已下线 | `curl 127.0.0.1:3425/v1/models` 重新选别名 |
| `unable to find container named` | 用了 Deployment 名当容器名 | 先 `kubectl get deploy X -o jsonpath='{.spec.template.spec.containers[*].name}'` |
| `approval_expired` / `target_drift` | 审批过期或目标已变 | 重新提交提案，不复用旧授权 |
| 执行后出现 followup 而非 verified | 动作没改善或样本不足 | 查待办原因，人工核实 |

## 7. 数据在哪、怎么清

- 业务数据：PostgreSQL `vigil` 库。事件/调查/假设/观测/工具账本/委派/提案/审批/执行/审计/待办/runbook 各一张表，迁移文件在 `src/aiops/migrations/`。
- 运行产物：`runtime/`（`VIGIL_RUNTIME_DIR`），存放大观测的原始文件，不入库。它是临时工作目录，不是证据存档。
- 验收记录：`faults/<场景>/runs/` 保存结论，`.evidence/` 保存不提交的本地截图。
- 历史遗留：一次演练会在库里留若干 `pending_approval` 提案与 `open` 待办，属正常记录；旧提案过期后再批准会因 `approval_expired` 拒绝。需要干净基线时可以重建库。

  > **警告**：下面的命令会删除 `vigil` 库中的全部业务数据和 checkpoint，无法恢复。只在确认不再需要现有记录时执行。

  ```bash
  docker exec vigil-postgres psql -U vigil -d vigil -c 'drop schema public cascade; create schema public;'
  ```

  清空后需要重新应用迁移；迁移入口随命令行一起重做，见设计文档。

## 8. 自检

```bash
uv run --frozen python scripts/skill_runtime_gate.py
```

`skill_runtime_gate.py` 检查网关 tool calling、结构化终态、checkpoint、Skill 信号匹配与只读子 Agent；失败项会写明原因，不要在未通过时宣称接入完成。
