# Vigil 安装与切换验收

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
本次以严格 msgpack 模式运行 `make check-all`，367 项通过，零跳过。Chrome 夹具 9 组通过，
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
