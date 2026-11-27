# Vigil 安装与切换验收

## 受控自动修复验收

在隔离 Kubernetes `v1.35.5+k3s1` 上运行 Vigil、Alertmanager、Prometheus 和真实业务探针。
API Server 启用 Metadata 级别审计。注入与复位由实验操作者执行。Vigil 的写入使用
`system:serviceaccount:vigil-monitoring:vigil-remediation`。

Docker 宿主使用 cgroup v1，实验节点以 `native` snapshotter 运行。部分固定镜像由已核对摘要
的本地副本导入，Prometheus 与 Alertmanager 分别使用 Debian 打包的 2.42.0 和 0.25.0。
以下证据保留各次运行的动作、会话、目标 UID、样本及对应审计 ID。
审计包含服务端预检，写入次数只计实际 PATCH。
准入正反例、专用身份核验和 Prometheus 规则校验输出单独归档。
[准入和监控证据](validation/controlled-remediation/admission-and-monitoring.txt)

| 场景 | 实际结果 | 证据 |
| --- | --- | --- |
| OOM 自动修复 | 现行容器内存限额 `16Mi`、三次 OOM 重启，首页 HTTP 500。将采集的 Alertmanager 通知重放至真实 Webhook 后，Vigil 只写一次内存限额到 `128Mi`。工作负载就绪约 61 秒后首页恢复，随后十个时段全通过，独立 OOM 信号为 `clear`，会话关闭。 | [网页](validation/controlled-remediation/oom.png) · [账本与审计](validation/controlled-remediation/01-oom-window.json) |
| CPU 限流自动修复 | 固定每秒一个真实首页请求。低限额 `5m` 下首页失败，限流周期占比超过 0.5。Vigil 只写一次 CPU limit 到 `200m`。首页恢复后十个时段全通过，窗内限流周期占比最高约 0.446，独立信号为 `clear`，会话关闭。将采集的 firing 和确认告警消失后的同身份恢复通知重放至真实 Webhook。 | [网页](validation/controlled-remediation/cpu.png) · [负载、账本与审计](validation/controlled-remediation/01-cpu-fixed.json) |
| 缺少 OOM 证据 | 首页 HTTP 500，当前容器运行在 `16Mi`、零重启。真实 Webhook 收到新告警身份后，Vigil 指出缺少 OOM 和重启证据，转人工审批。账本未授权发送，审计无目标写入。同时间段的扩容请求也保留在审计中。 | [现场、账本与审计](validation/controlled-remediation/02-insufficient.json) |
| 容量不足自动扩容 | 副本数从 1 增至 2，业务恢复，10 个时段全通过。会话关闭。一次写入。 | [网页](validation/controlled-remediation/scale.png) · [账本与审计](validation/controlled-remediation/04-scale-replay.json) |
| 普通 ConfigMap 修复 | `degraded → healthy`，应用加载新值，10 个时段全通过。会话关闭。一次写入。 | [网页](validation/controlled-remediation/configmap.png) · [账本与审计](validation/controlled-remediation/05-config.json) |
| 普通环境变量修复 | `VIGIL_MODE: degraded → healthy`，应用加载新值，10 个时段全通过。会话关闭。一次写入。 | [账本与审计](validation/controlled-remediation/05-env.json) |
| 恶化后补偿 | 失败率从 1/5 升至 5/5。第一次写成 `worsen`，第二次恢复 `degraded`。补偿卡和人工待办均落库。此场景将已采集的告警通过真实 Webhook 重放。 | [网页](validation/controlled-remediation/compensation.png) · [账本与审计](validation/controlled-remediation/03-compensation-fixed.json) |
| 网页批准普通配置 | AUTO 开启，`limited` 超出自动白名单，卡片显示“参数需人工审查”。Chrome 登录后批准 `VIGIL_MODE: healthy → limited`，后台返回 HTTP 200。API Server 记录一次写入，10 个时段全通过，独立加载信号为 `clear`。通过实验脚本调用实际提案入口创建卡片。 | [批准前](validation/controlled-remediation/manual-approval.png) · [批准后](validation/controlled-remediation/manual-applying.png) · [已验证](validation/controlled-remediation/manual-verified.png) · [账本与审计](validation/controlled-remediation/05-manual-policy.json) |
| 观察期间复发 | 首个恢复通知到达后会话继续观察。收窄 Service 的后端选择范围后，真实业务探针再次失败，Alertmanager 投递了第二次发作。两个发作最终均恢复，账本仍以无改善结算，十个时段中三个失败。仅初始扩容写入。 | [网页](validation/controlled-remediation/recurrence.png) · [告警、账本与审计](validation/controlled-remediation/06-recurrence-final.json) |

- **内存修复首轮。** 真实首页返回 HTTP 500，现行容器内存限额为 `16Mi`，三次重启，
  最后一次终止原因为 `OOMKilled`。Vigil 写入 `128Mi`，新 Pod 拉取镜像超时。
  账本记录变更生效超时。 [账本与审计](validation/controlled-remediation/01-oom.json)
- **内存修复第二轮。** Vigil 写入 `128Mi`，新 Pod 就绪后首页又过约 90 秒才恢复。
  这段时间造成前两个时段失败，原逻辑结算为无改善。实际首页恢复后持续返回 HTTP 200。
  [账本、探针与审计](validation/controlled-remediation/01-oom-final.json)
- **CPU 修复首轮。** Prometheus 返回 HTTP 400。Pod 名中的连字符被写成 PromQL 字符串不接受的转义，
  告警未获得发送权，API Server 审计只有身份自查。修复后使用运行中容器的 ID 取证，排除同一 Pod
  中已终止实例残留的指标。 [失败回包和审计](validation/controlled-remediation/01-cpu-fixed.json)
- **观察期间复发首轮。** 容量告警恢复后会话继续观察。实验操作者暂时收窄 Service 的
  后端选择范围，Alertmanager 投递了第二次发作，观察记录中出现两个失败时段。
  Prometheus 连接中断后动作以未知结果结束，只有初始扩容写入。 [账本与审计](validation/controlled-remediation/06-recurrence.json)

真实补偿首轮发现授权序列化加入了可选字段的 `null`，补偿在数据库读取后校验失败。
修复后增加持久化授权的回归用例，并在独立数据库复跑完整正向变更与补偿。
集群启动、注入和采集命令见[受控自动修复验收步骤](../env/README.md#11-受控自动修复验收)。

在独立 PostgreSQL 上运行 `LANGGRAPH_STRICT_MSGPACK=true make check-all`，Python 505 项、
Vitest 14 项和 Chrome 15 项全部通过，零跳过。门禁包括前端类型检查、样式检查和 Ruff。
迟到跨组告警的时间重叠修复另经 43 项 PostgreSQL 和 HTTP 聚焦回归验证。

## FastAPI 网页宿主验收

| 产物 | 构建提交 | wheel SHA-256 | 证据来源 |
| --- | --- | --- | --- |
| React + FastAPI | `aa6effdcb5e3ff9464dcc641cac0c03222c4d1b4` | `5b16b58d714e22c27c71077b8c5bbfa52ae326e1ceccae3872e4657cf0716881` | 本次构建、安装和实测 |
| React + Starlette 回退包 | `96ba75025e6336e90f7774ef03277886df7fc500` | `6b3c323893c1ca81157b4b19f9b640c6ba7a74207cb55479fa6521f5523b35bd` | 前置交付验收，本次复核文件摘要 |

新宿主基于 React 合并提交 `a1d612f85de65ab0b463fd7aa1aa6f2f4f90353c`。
运行时锁定 FastAPI 0.165.0、Starlette 1.10.0、Uvicorn 0.54.0、HTTPX 0.28.1、
AnyIO 4.16.0 和 Pydantic 2.14.2。在 CPython 3.12.14 和 3.13.15 分别从冻结锁解析并
安装同一 wheel。发布构建用 Node.js 26.13.0、npm 11.20.0 和 `uv build`，由 sdist
生成含生产 HTML、脚本、样式、内置 Skill 和迁移的 wheel。安装环境使用无 Node.js 的
`PATH`，在仓库外启动正式 `vigil serve`。

### HTTP、浏览器和双监听

- 干净源码解包后，在缺少 `web/dist` 时可编辑安装和一次性终端均成功；网页启动及普通
  wheel 构建报告 `make web-build`。
- 在独占 PostgreSQL 17.11 上以严格 msgpack 模式运行 `make check-all`。Python 372 项、
  Vitest 4 项、Chrome 夹具 10 项通过，零跳过。门禁内只构建一次前端，类型、样式与
  lint 同时通过。网页 API 和告警 TCP 快测 21 项通过。
- 使用正式 wheel 和 Chrome 157.0.8103.78 在两版 Python 上验证生产 CSP、同源静态资源、
  卡片深链接和刷新、一次性兑换和复放拒绝、Cookie 的 `HttpOnly`、`SameSite=Strict`
  和 `Path=/api`、只读拒绝及会话恢复。受控后端的批准、拒绝、关闭、消息响应丢失后同
  `input_id` 仅入账一次，以及 CLI 脱敏投影逐字段一致。交互和非交互服务收到 SIGTERM
  均以 0 退出。
- HTTP 回归覆盖 GET、HEAD、405 的 `Allow`、未知 API 和静态资源的 404、关闭默认文档
  和末尾斜杠跳转，以及对无效 JSON、过大请求、错媒体类型的共享错误响应。

### 真实处置与告警投递

新 k3d 节点启动时触达宿主 inotify 实例上限。本轮使用已核实的实验集群
`k3d-vigil-af-iybt7uz6`（k3d 5.9.0、Kubernetes 1.35.5+k3s1）及已有
`vigil-experiment` 命名空间，仅创建本任务唯一标签和名称的对象。数据库、受控模型和
AlertManager 0.30.0 均独立。Vigil 使用临时 ServiceAccount，具备所需的命名空间级读权限，
Deployment PATCH 按 `resourceNames` 限定唯一对象。现场用 `kubectl auth can-i`
验证目标 PATCH 允许、其他 Deployment 和命名空间拒绝，以及创建和删除拒绝。所有临时
对象都有 UID 记录，按 UID 前置条件清理。沿用实验集群的审计策略。

| 路径 | 实测口径 |
| --- | --- |
| 自然触发和恢复 | 在新 Deployment 注入故障让实际入口从 HTTP 200 变为 500。Blackbox Exporter 0.28.0 和 Prometheus 3.15.1 读到 500，规则经真实 AlertManager 向 Starlette 独立端口投递，创建会话。Chrome 从卡片链接登录并批准，正式执行器发出 1 次 dry-run 和 1 次实际 PATCH，均为 200。目标修订 2 → 3，故障配置被移除，HTTP 恢复 200。网页显示“已验证”，CLI 在同会话读取相同回执。Prometheus 规则恢复后，AlertManager 发送整组 resolved 并关闭原会话。 |
| 合成分组通知 | 经真实 AlertManager 的 `POST /api/v2/alerts` 注入同告警名的两个实例。两个实例进入同一会话，重发仍归入原会话，部分恢复保持打开，整组恢复关闭。 |
| 固定 TCP 夹具 | 快测通过真实 TCP 分别访问 FastAPI 和 Starlette，以固定 payload 覆盖并发归并、重启、部分和整组恢复及监听隔离。模型和集群写入使用替身。 |
| 正式进程重启 | 真实运行中网页端口拒绝告警路由，告警端口拒绝网页路由，未授权告警返回 401。交互式进程经 SIGTERM 退出后，在相同端口重启为非交互模式，两监听可达，网页只读，再次 SIGTERM 退出为 0。 |

最终验收的目标是 `vigil-experiment/Deployment/fastapi-5a0c0e2b55-web`。审计将
验收人注入故障的 PATCH 与 Vigil ServiceAccount 的两次请求分别记录。验证基线
7 个样本中 6 个失败，恢复后 7 个样本均通过，1 个 Ready Pod 且零重启。
`vigil --resume` 退出码为 0，18 条持久化事件与网页 API 逐字段相同。
AlertManager 记录 10 次 webhook 通知，零失败。

### 复跑与证据

在受测源码上，通过环境变量 `VIGIL_DATABASE_URL` 指定独占 PostgreSQL，再运行：

```bash
uv sync --frozen --group dev
LANGGRAPH_STRICT_MSGPACK=true uv run --frozen pytest -q tests/test_web_api.py tests/test_webhook.py
LANGGRAPH_STRICT_MSGPACK=true make check-all
uv build
uv export --frozen --no-dev --no-emit-project --no-hashes --output-file /tmp/vigil-requirements.txt
sha256sum dist/*.whl
```

本机脱敏验收资料保存在 `/home/zdan/.local/state/vigil/validation/fastapi-20261121/`，
包含两份 wheel、下表证据和复跑脚本，独立于 `/tmp` 保留，未纳入 Git。
主线程独立复跑的完整门禁、两版 Python 安装验收和真实处置证据保存在其
`parent-replay/` 子目录，实验目标为 `fastapi-784af8b59a-web`，同样记录一次实际 PATCH
及恢复关闭，并确认任务标签下零残余。
在该目录执行 `bash rerun.sh REPO all`，也可用 `gate`、`browser` 或 `cluster`。
脚本从固定提交解包，创建独占 PostgreSQL，锁定版本构建 wheel 并从仓库外启动。
`cluster` 核对实验集群、命名空间 UID 与审计策略后才创建资源，使用单独监听及按 UID
前置条件的清理。脚本需要本机 Chrome、Docker、kubectl、k3d 和该实验集群访问。

| 核对项 | 脱敏证据文件 |
| --- | --- |
| HTTP 与完整门禁 | `http.log`、`check-all.log` |
| Python 两版与安装包 | `sync312.log`、`sync313.log`、`wheel-install.log`、`wheel-sha256.txt`、`browser12/browser.log`、`browser13/browser.log` |
| 干净源码终端与构建边界 | `clean-once.txt`、`clean-missing-serve.txt`、`clean-missing-wheel.txt` |
| 实验目标、RBAC 和双监听 | `cluster-final/acceptance.log`、`cluster-final/objects.jsonl` |
| 实际写入与业务验证 | `cluster-final/audit-*.json`、`cluster-final/card-verified.png`、`cluster-final/web-events.json`、`cluster-final/cli-resume.jsonl` |
| 告警恢复和分组 | `cluster-final/natural-recovery.json`、`cluster-final/synthetic-group-events.json`、`cluster-final/am-metrics.txt` |

## 会话与旧库切换的历史验收

验证环境为独立 PostgreSQL 17.11、k3d v5.9.0 / Kubernetes v1.35.5+k3s1、
Python 3.12.14。浏览器用 Chrome 157.0.8103.78，调用方用 Claude Code 2.1.329。
验收库、浏览器和实验集群均使用本次独立配置。

## 受测版本与目标

| 用途 | 提交 | wheel SHA-256 |
| --- | --- | --- |
| 真实 Claude Code、交互终端和告警网页 | `a5f04c2a261fa1f3ca44c028f752341bd57187b3` | `54267cf611eedba23e570b6984a7c558936ad53f87343366f076b24fa3145c6b` |
| 跨进程接手、非交互服务、审批阻断、最终人工核实 | `178dd6604483f9516c3320c28266ffe5acc917e3` | `95484df220fe733ca06ada0fd92a7fcfaadf2ae5d16d8a71bedd8b9b9704ee14` |
| 旧库夹具 | `4765fb30198c72bb2a271c5da6679888fe59f399`，应用到 0011 | 源码运行 |

独立集群为 `k3d-vigil-af-iybt7uz6`，PostgreSQL 容器为
`vigil-agent-first-pg-iybt7uz6`。F01 目标为 `vigil-shop/Deployment/productcatalogservice`，
版本隔离和审批阻断使用 `vigil-experiment` 内新建的短期 Deployment。旧库切换在
`vigil_legacy_fixture` 的备份副本 `vigil_cutover_trial` 上执行。真实入口和跨进程场景
分别使用 `vigil_installed_trial`、`vigil_terminal_trial` 和 `vigil_takeover_trial`。

## 安装版的真实入口

构建 wheel 后在无源码目录启动，用 `vigil admin migrate` 升到 0014。
`vigil admin doctor` 确认五份内置 Skill 和完整迁移。网页与所有样式和脚本随 wheel 提供。

| 场景 | 操作和结果 |
| --- | --- |
| Claude Code 调用 | 用 Bash 执行安装版 `vigil -p "checkoutservice 现在健康吗？" --output stream-json`。末条 `result` 为 0。实时查询 Deployment、Pod 和 Warning 事件，期望与可用副本各 1，Pod 零重启。 |
| 交互终端 | 按 F01 方式发布故障版本。终端在查询时加入一条发布线索，账本有两条送达。核对回滚卡片后用 `/approve` 确认。审计记录 Vigil 的 dry-run 和一次实际写入，首页恢复 200，结果为 `verified`。 |
| 告警和网页 | AlertManager 的 firing 创建会话。安装版记录内置调查 Skill，Chrome 经一次性链接登录并批准实际回滚卡片。审计记录 dry-run 和一次实际写入，首页恢复 200。整组 resolved 关闭原会话，已获发送权的动作完成验证，页面显示“已验证”。 |
| 跨进程接手 | 安装版 `-p` 输出卡片链接，以 3 退出并释放租约。直接请求批准返回 403，Chrome 登录后批准。服务仅发出一次实际写入，`--resume` 的各条持久化事件与网页按 `session_id`、`seq` 逐字段一致。 |
| 非交互服务 | 后台 `vigil serve` 未打印登录链接，读取原样可用。同源写请求返回 403，收到 SIGTERM 后正常退出。 |
| 分组通知 | 在实际 AlertManager 注入同组的两项实验告警。部分恢复时原会话保持打开，整组恢复时关闭；再次触发产生新会话。 |
| Grafana | 原首页抓取与三条独立入口探测均成功。服务和节点查询实际 CPU、内存曲线，五张大盘加载。发布大盘能查询 F01 普通发布和 Vigil 回滚。 |
| 关闭和版本隔离 | 在独立 Deployment 实测回滚移除新增环境变量，保留两个副本。隔离注解后旧版本请求两次被 API 以 422 拒绝，使用有效版本的回滚成功。 |
| 审批阻断 | 在独立 Deployment 构造拒绝、过期和目标漂移。分别返回 `not_approved`、`expired` 和 `drift`。Kubernetes 审计没有记录 Vigil 对这三个动作发送的写请求。 |

实验上的首次终端调查没满足业务证据要求，执行器拒绝处置。重跑时明确要求核查入口失败
和 Deployment 历史，并使用另一份演练库，完成了批准、实际写入和验证。

### 复跑要点

1. 用与场景对应的提交构建 wheel，按[安装与配置](USAGE.md#1-安装与配置)准备独占库和
   实验集群端口转发。核对 kubeconfig 与 context 指向实验集群。运行 `vigil admin migrate`
   和 `vigil admin doctor`，确认迁移齐备且能发现五份内置 Skill。
2. 健康场景在 Claude Code 中用 Bash 执行
   `vigil -p "checkoutservice 现在健康吗？查询副本、重启和 Warning 事件" --output stream-json | tail -n 1`。
   核对工具收到的 `result` 和退出码 0。
3. 以[坏发布步骤](../faults/f01-bad-release/README.md#4-操作步骤)单独执行注入，
   核对首页 500 和 `frontend-probe`。交互终端运行 `vigil "首页返回 500"`，出现查询时
   输入发布线索。核对送达事件和卡片后按终端提示批准。告警场景用同一注入方式，
   在交互式 `vigil serve` 启动后等自然 firing，再在 Chrome 登录和批准。
   每轮复位前核对首页 200、坏 ReplicaSet 为零副本、动作 `verified` 和实际 PATCH 次数。
4. 跨进程场景使用 `vigil -p "首页返回 500，请先调查再提回滚卡片" --output stream-json`，
   以 3 退出后用网页链接打开该卡片。核对未登录批准返回 403。登录批准后执行
   `vigil --resume SESSION_ID --output stream-json`，将持久化序号和同会话网页事件比对。
5. 分组通知场景经实验 AlertManager 的 `POST /api/v2/alerts` 发入同告警名、不同
   instance 的两条假数据，依次设第一条和第二条已恢复，再次触发一条。
   该场景使用真实接收服务和 AlertManager；第 3 步使用真实首页故障自然产生的告警，
   [告警 TCP 验证](../tests/test_webhook.py)使用固定 payload。

本地保留的脱敏证据按场景索引。审计仅留对象、时间、dry-run 和结果码。

| 场景 | 证据文件 |
| --- | --- |
| Claude Code 实际 Bash 与末行 | `claude-vigil-tail-outer.jsonl` |
| 终端、真实告警、跨进程接手 | `scenario-b-audit.log`、`scenario-c-audit.log`、`takeover-audit.log`、`takeover-resumed.jsonl` |
| AlertManager 真投递及浏览器结果 | `scenario-c-events.json`、`alertmanager-roundtrip.log`、`browser-evidence/alert-card-verified.png` |
| 阻断、隔离与旧库 | `negative-approval-evidence.log`、`action-real-k3d-adapter.log`、`cutover-status.log`、`cutover-reconcile-final.log` |
| 门禁和浏览器夹具 | `release-check-all.log`、`installed-frontend-fixture.log` |

## 并发与异常回归

以下测试显式传入独占库连接。集群适配器故障用可暂停的受控替身，保留提交和真实写请求的
区分。测试完成后确认输出的通过和跳过数。

```bash
uv sync --frozen --group dev
LANGGRAPH_STRICT_MSGPACK=true make check-all
```

| 边界 | 回归位置 |
| --- | --- |
| 两进程竞争、续租前后 checkpoint 写入、旧代次不能补写、投递与 checkpoint 同事务 | [会话账本](../tests/test_sessions.py) |
| 运行中插话、旁路继续、重新查询、崩溃后区分已有或未保存的查询 | [会话对话](../tests/test_agent_conversation.py)、[调查恢复](../tests/test_agent_recovery.py) |
| 审批拒绝或过期、预算阻断、跨进程决定、自动重建策略 | [动作与批准](../tests/test_agent_actions.py)、[终端](../tests/test_cli.py) |
| 关闭与发送竞争、旧驱动不能领取动作、活跃验证持续续锁、原发送者可能运行时拒绝人工解锁 | [处置安全](../tests/test_action_safety.py)、[网页 API](../tests/test_web_api.py) |
| 连接断开、重定向、发送权前后中断、未知结果保锁 | [集群 HTTP 适配](../tests/test_action_kubernetes_transport.py)、[处置安全](../tests/test_action_safety.py) |
| 重复告警、部分恢复、整组恢复后再触发和独立监听 | [告警 TCP 验证](../tests/test_webhook.py) |
| 两档视口、四态布局、十一条卡片分支、同源资产与脱敏投影 | [浏览器验证](../tests/web/browser_cases.py)、[样式检查](../tests/web/check_styles.py) |

305 秒持租约由真实 PostgreSQL 验证。动作锁续期覆盖长于首个租期的验证，测试缩短了时钟
以验证接手竞争。界面矩阵使用夹具驱动各态，真实网页验收使用安装版服务与真实集群。
会话切换交付以严格 msgpack 模式运行 `make check-all`，367 项通过，零跳过。Chrome 夹具 9 组通过，
包含 11 条卡片状态路径。

## 旧库切换预演

从已提交的旧版构建四类遗留动作夹具，保留历史调查、已存在的旧锁和 LangGraph checkpoint。
先关闭写入夹具的进程，备份独占数据库并恢复到另一独占库，再对备份副本运行新版迁移。
运行切换预览，注入未过期的旧租约，确认执行拒绝且记录不变。令租约过期后执行切换，待审、
已批准未发送和 intent 三类被作废，可能已发送的动作转为 unknown 并保留目标锁。再次执行
只保留原处置与锁。历史报告和 checkpoint 仍可读取。

使用安装版的 `vigil admin reconcile-action` 核实原部署。第一次运行保留了锁，Kubernetes
控制器在隔离后推进了版本。修复后的安装版等待控制器确认 generation，真实核实将这条
未发送的演练记录结为 failed 并释放目标锁。

精确命令与发布操作边界见 [操作手册](USAGE.md)。
