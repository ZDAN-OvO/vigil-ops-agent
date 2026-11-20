# 启动本地网页服务并把告警归入可接手的会话

## 目标

网页能读取、发消息及恢复本地会话。具备浏览器凭证的人能在网页处置卡片。告警按组进入会话，恢复时关闭该会话并处理未发送的卡片。

## 本期不做

- 浏览器页面和右栏采集由独立任务实现。不开放公网访问或多账户登录。
- webhook 服务只挂告警接收职责，不暴露会话和审批路由。

## 完成标准

- `serve` 生命周期管理本地网页监听、单独的 webhook 监听和持久化队列驱动。双方监听隔离。网页绑定 loopback。webhook 绑定显式配置的实验网关，用 Bearer 恒定时间比较，旧头和缺少令牌均拒绝。
- 浏览器服务提供列表、会话快照、`after_seq` 续读、发消息、恢复、关闭告警会话、卡片决定和右栏快照。人工关闭使用与批准相同的网页凭证和同源保护，避免读者间接作废他人待审卡片。关闭接口幂等，关闭后的发消息和恢复请求返回已关闭。列表和快照包含设计显示所需状态。输出使用共享脱敏投影。会话事件中的卡片链接使用本地网页地址、会话 ID 和卡片锚点，页面打开后定位对应卡片。该地址取本任务提供的 `web_url` 配置。只接受本机网页源，默认不配置，此时事件的 `url` 为 null。服务与网页任务一起固定会话路径和卡片锚点。非终端启动仍可使用该地址查看。断线后续读按序号去重。
- stdin、stdout 均为终端时发一次性登录链接，消费后重定向至不含凭证的页面，写 `HttpOnly` 和 `SameSite=Strict` Cookie。决定请求同时查 Cookie 和同源，且要求卡片 ID 和指纹。凭证、链接与 cookie 不进可读取的出站事件、访问日志或错误返回。非终端启动时不发链接，服务只开放网页读取。网页会话快照用 `approval_enabled` 表示当前请求是否通过审批入口的授权检查，包括启动模式和 Cookie，并返回适合展示的禁用原因。同源、卡片状态和指纹在决定请求到达时重新校验。
- 调用会话存储的按组事务接口，按 `groupKey` 串行处理。部分唯一索引保证仅一个未关闭会话。一次首次 firing 持久化会话、首条消息和待跑轮。重复 firing 加事件且不启动新轮。组内部分恢复加提示，整组 resolved 或人工关闭触发关闭。提交后响应，重启后有驱动者接手。
- resolved 和人工关闭调用动作任务的原子关闭接口。关闭与发送权按会话行锁互斥。先取得发送权的动作独立收尾，动作回执留在同一已关闭会话。
- 终端持有租约时由终端消费审批。无人持租约时 `serve` 持久化扫描并接手。批准、拒绝或作废通知到达前后停机，重启能继续原轮或完成关闭。旁路执行不占主线租约。

## 关键取舍

- 优先复用 Starlette 和 uvicorn。会话续读按 `after_seq`，网页可用短轮询驱动，审批响应仅读已提交结果。
- 启用告警恢复通知与新接入同步协调。AlertManager 清单由监控任务统一修改，本任务交付所需 Bearer 配置、监听地址、迁移时序和测试样例。

## 前置依赖

- [对话运行与恢复](TODO_conversation.md) 的轮次、提交、关闭后的调用停止和恢复。
- [会话账本与租约](TODO_session-store.md) 提供告警组存储，由对话任务带入集成基线。
- [卡片授权与安全处置](TODO_action-safety.md) 的卡片决定、发送与关闭事务、出站投影。审批写入口按总纲的决策开通。
- 值班面板联调使用 [值班数据与 Grafana](TODO_monitoring.md) 的数据接口。其余端点可用替身先验收。

## 已核实事实

- `src/aiops/webhook.py:85` 只处理 firing，`:180` 同时收旧头和 Bearer，`:201` 是现行端点，`:250` 直接启动进程内任务，`:264` 是 Starlette 应用工厂。
- `env/manifests/monitoring.yaml:72` 按 `alertname` 分组，`:86` 禁用 resolved。
- `tests/test_webhook.py:126` 覆盖旧去重，`:164` 覆盖缺凭证。重建用例沿用真实库闭环。

## 变更范围

- 修改 `src/aiops/webhook.py`，新增 `src/aiops/serve.py` 和网页 API 模块。保持网页和 webhook 独立路由与监听生命周期。
- 协议与网页任务对齐后把正式静态资源交给页面任务，和 CLI 对齐 `serve` 入口。在 `src/aiops/config.py` 定义网页源和监听配置，同步 `.env.example`。凭据取本地配置。

## 风险与回滚

- 请求确认后首轮没入队会漏调查。归并、入队和关闭用事务，操作完成后再确认接收。
- 关闭入口只停止新轮，已取得发送权的动作按执行器收尾。退回旧 webhook 前核对待跑轮、认证配置和未结束动作。

## 验证

- 在独立库运行 `uv run --frozen pytest -q tests/test_webhook.py tests/test_agent_recovery.py`，新增 API、监听、Cookie 同源、并发关闭、旧头拒绝和重启回归。从网页人工关闭入口验证关闭先提交时，待审卡片作废，dry-run 中的动作无法取得发送权。
- 局部验收直接向监听地址提交 firing、部分与整组 resolved 样例，核对归并、关闭和关闭后再次 firing。交付 AlertManager 清单所需设置和样例。由 [集成与切换验收](TODO_release-verification.md) 在监控任务更新清单后实际接收 k3d AlertManager 推送，并检查跨进程审批。

## 停止条件

- 网页露出非本地监听、无凭证决定生效、凭证进日志，或关闭后仍新取发送权时暂停接入。
- AlertManager 主机到 webhook 的路由或凭证无法在隔离环境确认时，停在告警切换前。

## 进度

- [x] 监听与只读会话 API
- [x] 审批入口和租约接手
- [x] 告警归并、关闭与恢复

## 范围外的问题

远程运维接入和用户身份体系留待另案。

## 服务接入

CLI 调用 `await aiops.serve.run_serve(settings, interactive=stdin_is_tty and stdout_is_tty)`。
服务先运行 `Store.migrate()` 和 `setup_checkpoints(store)`，再使用
`aiops.investigation.factory.open_conversation(settings=settings, store=store)` 提供的
`ConversationDriver`。生产装配须把 `settings.web_url` 传给发布卡片的
`ActionExecutor`。未配置时，事件的 `url` 为 `null`。

| 配置 | 作用 |
|---|---|
| `VIGIL_WEB_PORT=8090` | 网页监听端口，主机固定为 `127.0.0.1`。 |
| `VIGIL_WEB_URL=http://127.0.0.1:8090` | 可选。事件将链接到 `/sessions/{session_id}#card-{card_id}`。必须与实际网页源一致。 |
| `VIGIL_WEBHOOK_HOST=172.20.0.1` | 实验网关在宿主机上的具体 IP。 |
| `VIGIL_WEBHOOK_PORT=8091` | 告警端口。须同步修改 AlertManager 接收地址。 |
| `VIGIL_WEBHOOK_TOKEN` | 与 AlertManager 令牌文件内容一致。 |

`VIGIL_WEBHOOK_HOST` 与 `VIGIL_WEBHOOK_TOKEN` 均省略时只启动网页。
同时部署网页与告警时两项均需提供，监听地址不可使用通配地址。迁移新增的
`session_inputs.alert_names` 对历史输入默认空数组，只在告警首轮持久化结构化名称，
供 Agent 匹配 Skill。

### 网页 HTTP

`aiops.web_api.build_web_app(settings, sessions=..., safety=..., auth=..., monitoring=...)`
返回 Starlette。前端从同一地址提供 `/`、`/sessions/{session_id}` 和静态资源；
卡片 DOM 锚点为 `card-{card_id}`。监控任务传入无参异步可调用对象
`monitoring.snapshot`。缺少采集装配时，`/api/monitoring` 返回六个 `missing` 块。

| 方法与路径 | 请求与成功响应 |
|---|---|
| `GET /api/sessions` | `limit` 默认 50，范围 1–200。200 `{sessions: Session[]}`，按更新倒序。 |
| `POST /api/sessions` | 无请求体。201 `{session: Session}`，新建一条可输入的用户会话。 |
| `GET /api/sessions/{session_id}` | 200 `{session: Session, approval_enabled: boolean, approval_disabled_reason: string\|null}`。 |
| `GET /api/sessions/{session_id}/events` | `after_seq` 默认 0，`limit` 默认 100（1–200）。200 `{events: SessionEvent[], next_after_seq: number, has_more: boolean}`。 |
| `POST /api/sessions/{session_id}/messages` | JSON `{text: string, input_id?: string}`，202 `{input_id, queued_seq}`。同一会话以同一 ID、同一文本重试会返回原序号。 |
| `POST /api/sessions/{session_id}/resume` | 202 `{session: Session}`。驱动扫描已经持久化的工作，无请求体即可。 |
| `POST /api/sessions/{session_id}/close` | 200 `{session: Session}`。仅告警会话，无请求体即可，重复关闭只返回当前状态。 |
| `POST /api/sessions/{session_id}/cards/{card_id}/decision` | JSON `{decision: "approved"\|"rejected", fingerprint: string, reason?: string}`，200 `{decision_id, status}`。 |
| `GET /api/monitoring` | 200 监控快照。告警组按原始 `group_key` 关联未关闭会话，`groups[].session` 为 `{session_id, pending_approval}` 或 `null`。 |

`Session` 示例：

```json
{
  "id": "3697b83f8aaf45f79436e704e7ac74f7",
  "source": "alert",
  "title": "FrontendProbeFailed",
  "state": "awaiting_approval",
  "last_seq": 12,
  "created_at": "2026-11-21T01:30:00+00:00",
  "updated_at": "2026-11-21T01:30:30+00:00",
  "closed_at": null,
  "pending_approval": true
}
```

`SessionEvent` 按 `SessionEvent.as_dict()` 展平，公共字段为
`type, session_id, seq, ts, turn_id`，其余按事件类型。客户端按 `seq` 去重，用
`next_after_seq` 续读，有 `has_more` 时可立即读下一页。消息、卡片、动作回执以提交后的
事件为准。网页允许读取已关闭会话与后到的处置回执。

终端提供的登录链接使用浏览器片段，登录页清除片段后再用同源 POST 兑换 Cookie，
成功以 303 跳转到 `/`。Cookie 仅用于 `/api`，设置 `HttpOnly` 和
`SameSite=Strict`，当前服务进程有效。全部网页写请求同时核对 `Host`、
`Origin` 与 Cookie。非交互启动时，网页仅开放读取。错误响应为 `{error, message}`，
常用状态为 400（字段错误）、403（凭证或来源无效）、404（记录不存在）、
409（例如 `session_closed`、`not_pending`、`hash_mismatch`）。

### AlertManager 接收端

把下面的配置接到实验清单现有告警路由，并在停止旧服务及完成数据库切换后启用。
接收路径精确为 `POST http://172.20.0.1:8091/webhook/alertmanager`，请求类型
`application/json`，认证头为 `Authorization: Bearer <令牌>`。使用集群中挂载的令牌文件，
令牌只由本地和集群的配置注入。若实验网关 IP 或端口不同，需同步两侧。

```yaml
webhook_configs:
  - url: http://172.20.0.1:8091/webhook/alertmanager
    send_resolved: true
    http_config:
      authorization:
        type: Bearer
        credentials_file: /etc/alertmanager/secrets/token
```

下列 JSON 给出按 `alertname` 分组的三次推送。先发首次 firing，重复同一条时追加提示并
返回同一会话 ID。随后发部分恢复及整组恢复。AlertManager 发送的额外字段可以保留。

首次 firing：

```json
{
  "groupKey": "{}/{alertname=\"FrontendProbeFailed\"}:{alertname=\"FrontendProbeFailed\"}",
  "status": "firing",
  "alerts": [
    {"status": "firing", "labels": {"alertname": "FrontendProbeFailed", "instance": "shop-a"}, "annotations": {"summary": "首页探测失败"}},
    {"status": "firing", "labels": {"alertname": "FrontendProbeFailed", "instance": "shop-b"}, "annotations": {"summary": "首页探测失败"}}
  ]
}
```

部分恢复，组状态仍为 firing：

```json
{
  "groupKey": "{}/{alertname=\"FrontendProbeFailed\"}:{alertname=\"FrontendProbeFailed\"}",
  "status": "firing",
  "alerts": [
    {"status": "resolved", "labels": {"alertname": "FrontendProbeFailed", "instance": "shop-a"}, "annotations": {}},
    {"status": "firing", "labels": {"alertname": "FrontendProbeFailed", "instance": "shop-b"}, "annotations": {}}
  ]
}
```

整组恢复：

```json
{
  "groupKey": "{}/{alertname=\"FrontendProbeFailed\"}:{alertname=\"FrontendProbeFailed\"}",
  "status": "resolved",
  "alerts": [
    {"status": "resolved", "labels": {"alertname": "FrontendProbeFailed", "instance": "shop-a"}, "annotations": {}},
    {"status": "resolved", "labels": {"alertname": "FrontendProbeFailed", "instance": "shop-b"}, "annotations": {}}
  ]
}
```

成功响应 200 `{session_id: string|null, outcome: string}`，其中 `outcome` 为
`created`、`repeated`、`partially_resolved`、`closed` 或 `ignored`。
`ignored` 时 `session_id` 为 `null`。整组恢复后再次 firing 会得到新的会话 ID。
