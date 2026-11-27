# Vigil 操作手册

## 1. 安装与配置

在源码目录运行 `uv sync --frozen --group dev`，以下命令可加 `uv run --frozen` 前缀。
源码安装后即可使用终端命令。使用 Node.js 26 和 npm 执行 `make web-build` 后，
`vigil serve` 从 `web/dist` 读取网页。发布时先构建网页，再生成带静态资源的 wheel：

```bash
make web-build
uv build
```

把 wheel 安装到独立的 Python 3.12 或 3.13 环境后，可以直接运行终端和网页。
wheel 自带网页资源，运行环境只需 Python：

```bash
uv venv --python 3.12 /path/to/vigil-env
uv pip install --python /path/to/vigil-env/bin/python dist/vigil_aiops-0.1.0-py3-none-any.whl
```

从源码运行时，配置在仓库的 `.env`。安装版使用 `$XDG_CONFIG_HOME/vigil/.env`，
默认是 `~/.config/vigil/.env`。同名环境变量优先。复制仓库的 `.env.example` 后填入
本机设置。数据库连接串和令牌保存在本地受限文件中。

| 配置 | 用途 |
| --- | --- |
| `VIGIL_DATABASE_URL` | 指向用于会话、审批与 checkpoint 的 PostgreSQL |
| `VIGIL_KUBECONFIG`、`VIGIL_KUBE_CONTEXT` | 实验集群的 kubeconfig 和 context |
| `VIGIL_CLUSTER`、`VIGIL_NAMESPACE` | 调查范围与记录身份 |
| `VIGIL_MODEL_BASE_URL`、`VIGIL_MODEL_NAME` | OpenAI 兼容网关地址和已启用的模型 ID |
| `VIGIL_PROMETHEUS_URL`、`VIGIL_ALERTMANAGER_URL` | 默认 `http://127.0.0.1:9090` 与 `http://127.0.0.1:9093` |
| `VIGIL_WEB_PORT` | 本机网页端口，默认 8090 |
| `VIGIL_WEB_URL` | 可选。与本机网页源相同，例如 `http://127.0.0.1:8090`，用于生成卡片链接 |
| `VIGIL_GRAFANA_URL` | 可选。配置后值班面板提供大盘链接 |
| `VIGIL_SKILLS_DIR`、`VIGIL_RUNTIME_DIR` | 可选。覆盖内置 Skill 目录和运行时观测文件存放位置 |

接入 AlertManager 时额外配置 `VIGIL_WEBHOOK_HOST`、`VIGIL_WEBHOOK_PORT` 和
`VIGIL_WEBHOOK_TOKEN`。监听主机填实验 k3d 的具体网关地址，令牌和 AlertManager 的
Bearer 文件使用同一值。部署方式见[实验环境](../env/README.md#8-告警入口)。

新装或升级后，先执行迁移，再启动新版 `vigil serve`。0014 及更早的库会添加标题来源，已有的人工和告警标题会保留。迁移后用 `vigil sessions` 和网页会话列表核对旧标题。用 `vigil admin doctor` 检查配置和内置 Skill。

```bash
vigil admin migrate
vigil admin doctor
```

## 2. 对话与恢复

| 命令 | 用途 |
| --- | --- |
| `vigil` 或 `vigil "首页打不开"` | 在交互终端新建会话，可带首条消息 |
| `vigil -p "checkoutservice 有几个 Ready 副本？"` | 运行一轮并退出 |
| `vigil --resume SESSION_ID`、`vigil -c` | 恢复指定会话或最近会话，可加 `-p` 追问 |
| `vigil sessions` | 列出用户会话和告警会话 |
| `vigil serve` | 启动本机网页，以及已配置的告警监听 |
| `vigil admin --help` | 查看迁移、诊断、runbook、待办与人工核实命令 |

调查正在运行时可以追加线索。看到“已送入”后，线索已保存进本轮 checkpoint。
会话等待审批时，另一个进程用 `vigil -p "现在几个副本？" --resume SESSION_ID`
可以走独立旁路。会话和事件支持在 CLI 与网页互相接手。

`vigil serve` 通过 FastAPI 在 `127.0.0.1` 同源提供页面和会话 API。可以打开并
刷新 `/sessions/SESSION_ID`，卡片链接会定位到对应审批卡。在浏览器访问时请使用
`http://127.0.0.1` 和启动端口。后台启动时网页只读，告警从单独配置的监听接收。

### 在网页调查

- 在运行中继续发送线索。消息先显示已排队，送达后显示实际领取它的主线或旁路以及步骤。
- 在假设下看关联依据。点 **查看观测** 跳到采集时间、查询范围和脱敏结果。展开 **判断历史** 查看之前的状态和依据。展开 **调查过程** 查看查询和 Skill。展开一条 Skill 记录可查看该次保存的正文。
- 点会话标题或 **改名**，再点 **保存** 或 **取消**。新网页会话从首条问题取名，保存人工名称后以人工名称为准。
- 拖动两条分割线，或聚焦后用方向键调整。点 **恢复默认分栏** 重置宽度和收起状态。窄屏从 **会话** 和 **值班面板** 打开侧栏，按 Escape 可关闭。

### 读取命令行结果

`--output text` 是默认输出。供脚本读取时用 `--output json` 获取本次结果与卡片，
或用 `--output stream-json` 获取逐行 JSON。最后一行始终是 `result`。数据库事件的
`seq` 在同一会话中递增，客户端用 `session_id` 与 `seq` 去重。连接级恢复提示或本地
结束信息用 `seq: null` 和 `persisted: false`。

| 退出码 | 含义 |
| --- | --- |
| 0 | 本轮完成或已读取结束状态 |
| 1 | 运行业务或配置错误 |
| 2 | 参数错误 |
| 3 | 有待审批卡片，或从待审批会话启动的旁路已结束 |
| 4 | 预算耗尽，结论不完整 |

### 审批和验证

在交互终端核对目标、变更及验证方式，然后输入卡片显示的 `/approve XXXX` 或
`/reject XXXX`。网页从交互式 `vigil serve` 启动时，打开终端打印的一次性链接登录，
在卡片上点击批准或拒绝。没有网页登录凭证时，页面可以查看会话和值班面板。

非交互输入和机器输出只返回卡片与查看地址，批准和拒绝仅由交互终端或已登录浏览器完成。
受控动作包括登记工作负载的 CPU 和内存规格、副本数，以及普通环境变量和 ConfigMap。
配置 `VIGIL_REMEDIATION_CONFIG_DIR` 和 `VIGIL_ACTION_KUBECONFIG` 后才能发起写入。
专用身份、准入清单和权限核对见 [实验环境](../env/README.md#11-受控自动修复验收)。
授权默认有效 900 秒。自动处置默认关闭，启用 `VIGIL_REMEDIATION_AUTO_ENABLED=true`
后，只有已登记规则及现时证据允许自动发送。原 Pod 重建和整模板回滚的历史记录可读。
收到授权过期或目标已变的结果后，应重新调查并生成符合当前目标的新卡片。

## 3. 告警

AlertManager 的首次 firing 为同一 `groupKey` 创建会话和首轮调查。自动处置还会核对
`fingerprint`、`startsAt` 和登记目标。同一故障最多各发一次自动处置和自动补偿。
整组恢复时，正在处置的会话继续观察。默认观察 10 分钟，每分钟至少取得一次真实业务探针结果。
只有探针全过、同类故障消失并且告警组已恢复，才自动结束会话。

CPU 或内存调整确认工作负载就绪后，业务探针还有一段收敛时间。
收敛时长和等待变更生效的时长分别计时，共用 `VIGIL_ACTION_ROLLOUT_TIMEOUT_SECONDS`，
默认各 180 秒。Vigil 保存收敛期限内的首个成功探针时间，从该时刻开始计算完整观察窗。
此后探针失败会计入同一观察窗。生效后的前五个时段仍用于判断恶化及补偿，
同类告警也从生效时起核查。收敛超时或缺少核实证据时，Vigil 转人工核实。
副本数和配置变更的观察窗从生效时开始。

关闭会话时，尚未取得发送权的动作作废。已取得发送权的动作继续验证并记录回执。
结果为不确定时保留目标锁，核实后才能再次处置该目标。

## 4. 人工核实和旧库切换

用 `vigil admin followups` 查待办。在实际管理终端运行
`vigil admin reconcile-action CARD_ID`，按提示核对原执行者已退出或超过等待时限。
核实先推进实际写入对象的资源版本，再读取限定字段的状态。确认是否已经写入及验证结果，
记录结论和依据。隔离失败或状态无法确定时，保留目标锁。

旧版升级采用停机切换。先停旧 webhook 和驱动，确认进程已退出且禁止自动重启。备份
PostgreSQL，并将备份恢复到独立库验证可读。

若旧库仍在 0010 或更早版本，先导出需要保留的调查、证据和工具调用记录。
[0011 迁移](../src/aiops/migrations/0011_skills_and_delegations.sql) 会删除
`hypothesis_revisions`、`evidence` 和 `tool_calls`，旧证据不会转换为 `observations`。
需要查阅原报告时，升级前用旧版在备份副本上核对并导出。验收预演的起始库已在 0011。

对切换目标运行 `vigil admin migrate`，然后先预览，再实际执行：

```bash
vigil admin prepare-legacy-cutover
vigil admin prepare-legacy-cutover --apply --confirm \
  --old-processes-stopped --restart-disabled --backup-complete
```

活跃旧租约或无法分类的记录会阻断执行。确认旧驱动状态并核对遗留动作后再运行。
已确认未发送的旧提案会作废，可能已发送的动作保留目标锁，进入人工核实。处理未结动作后再切换
AlertManager 入口。

如要恢复旧版，先停止新接入和驱动，保留切换后的会话、授权、尝试和目标锁并核对集群。
有未核实写入或请求仍在途时保持停写。数据库恢复前须确认旧授权不会重新变为可执行。

## 5. 常见问题

| 现象 | 检查 |
| --- | --- |
| 数据库连接异常 | 确认指向预期库，运行 `vigil admin doctor` |
| 网站可读但无法写入 | 确认 `vigil serve` 从交互终端启动，并在当前浏览器打开其一次性链接 |
| 告警 webhook 返回 401 | 核对两个服务的 Bearer 配置 |
| Prometheus 查询失败 | 核对端口转发是否仍绑定当前 Pod |
| 值班面板某块缺数据 | 检查该数据源。Prometheus、AlertManager、Kubernetes 和 Grafana 的配置见[实验部署](../env/README.md#10-值班数据与-grafana) |
| `unknown` 或人工待办 | 从 `vigil admin followups` 找对应动作，核对原发送者和集群结果后人工处理 |

源码环境可以用 `make check-all` 检查代码。要覆盖数据库路径，请明确设置独占的
`VIGIL_DATABASE_URL`。网页检查需安装 Chrome。只运行网页门禁时使用：

```bash
make web-test
```
