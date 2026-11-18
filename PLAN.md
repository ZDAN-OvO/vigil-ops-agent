# PLAN：Web 对话首版实现

- 分支：`feat/web-conversation`（基于 `origin/main` 6f14e84）
- 设计依据：`DESIGN.md`（本分支唯一设计文档；本计划只做切片与验收，不改设计）
- 本文件在 PR 合入前删除。

## 0. 总规则

1. **以 DESIGN.md 为准**。本计划与 DESIGN.md 冲突时以 DESIGN.md 为准，并在提交说明中写明冲突点。
2. **按里程碑顺序做**，每个里程碑一个或多个提交，提交信息用简体中文、以 `feat(web):` / `fix(...)` / `test(...)` 开头。每个里程碑结束时 `make check-all` 必须绿，`git diff --exit-code` 无格式改写。
3. **测试先行**：附录 A.15 的每一条断言都要有对应测试（下面每个里程碑列出归属）。需要 PostgreSQL 的测试沿用现有 `VIGIL_DATABASE_URL` 自动跳过机制；本机 `.env` 已配实验库，本地必须实际跑到，不允许全部跳过。
4. **不可协商的安全底线**（任何里程碑都不能放宽，评审重点查）：
   - Web 只监听 `127.0.0.1` / `::1`；Host 校验（421）、写请求 Origin 校验（403）、`frame-ancestors 'none'` + `X-Frame-Options: DENY`。
   - Web 应用与告警应用是两个独立应用对象，告警监听口访问 `/api` 和页面返回 404。
   - 真实写集群请求只发一次，关闭传输层重试；超时/连接错误/5xx 记 `uncertain` 并生成待办。
   - 审批只走结构化接口，聊天文字不产生审批效果；`card_hash` 必须校验。
   - `serve` 下告警调查只读、Pod 重建自动执行停用；页面调查只能 `rollout_undo` 且必须人工审批。
   - Web 响应只含 A.11 字段，自由文本经 A.13 过滤。
5. **不做**：DESIGN 第 10 节所列内容；不新增动作类型；不重构与本设计无关的模块。

## 1. 里程碑

### M1 CLI 收敛与单一推进者锁（DESIGN 第 7 节、A.2 前两条）

改动：
- `src/aiops/cli.py`：顶层只保留 `serve`、`doctor`；其余命令（含 `db`、`lab` 子组）移入隐藏的 `dev` 子组，行为不变。
- 新增进程文件锁 `runtime/vigil.lock`（`fcntl.flock` 排他，持有到进程退出）。`serve` 与 A.2 列出的 `dev` 写命令取锁；`dev followup resolve` 不取锁。
- `serve` 启动顺序按 DESIGN A.2「启动顺序与未决写入」三步：命令行来源或归属不明的未决执行 → 拒绝启动；页面会话的未决执行 → 开放监听前按 A.6 第 3 行只写库恢复为待核实；再查一次，通过才开放监听。M1 先实现第 1、3 步与"无页面会话时即拒绝"的判断（M2 前还没有会话表，所有未决执行都视为不属于页面会话）；第 2 步的恢复逻辑在 M4 接入，并补"页面执行崩溃后重启"测试。
- 更新 `README.md`、`docs/USAGE.md`、`Makefile` 的 `help` 及所有脚本（`faults/**/repeat.sh`、`scripts/`）中对旧命令的调用，改为 `aiops dev ...`。
- `doctor` 先提供骨架：数据库、模型网关、集群、Prometheus 检查与修复提示；探针、mutating webhook、页面文件检查在 M3/M6 补齐。

测试（A.15「推进者」中与锁相关的部分）：`aiops --help` 只列 `serve`/`doctor`；`serve` 运行时 `dev approve` 被拒；写命令运行时 `serve` 启动失败；锁随进程不随数据库连接；存在命令行来源的未决执行记录时 `serve` 拒绝启动；现有 `test_cli.py` 全部迁移到 `dev` 路径后仍绿。

完成标准：上述测试绿；`grep -rn "aiops \(investigate\|approve\|resume\|act\|lab\|db\)" README.md docs scripts faults` 只剩 `aiops dev ...` 形式（运行记录 `faults/*/runs/` 中的历史原文不改）。

### M2 数据模型与会话持久化（A.9、A.14、A.6 工作表）

改动：
- 新迁移 `0013_web_conversation.sql`：会话表（未结束列部分唯一索引）、会话消息表、请求防重表、工作表（`queued`/`running`/`done`）、会话事件表（自增 ID）、`serve` 运行标记表；任务表加 `ready_proposal_id`、`actions_disabled`。
- `db.py`：创建会话事务（会话、初始消息、事件、调查、任务、启动工作、防重记录）；追加线索消息；工作领取（同事务取租约）；事件写入与按 ID 读取；`finish_attempt` 改为 `result` jsonb 合并更新。

测试：A.15「防重」全部；并发创建只成功一个会话；`finish_attempt` 合并更新不覆盖已写 `send_state`；迁移可在空库与现有库上执行。

完成标准：测试绿；`aiops dev db migrate` 在实验库执行成功（记录输出）。

### M3 执行器与回滚内容固定（A.3、A.4、A.6 执行记录与锁、A.7 中执行器入口）

改动：
- `actions/rollout_undo.py`：提案参数写入历史 ReplicaSet UID/revision/控制器 UID、清理后完整目标 Pod 模板、镜像与 revision、变更清单、探针查询，并纳入 `proposal_hash`；全部基于原始 JSON；写入改为 `replace_namespaced_deployment` 只替换 `spec.template`，不再写 `change-cause`；dry-run 核对；写后回读 `template_mismatch`；真实写请求关闭重试，按状态码分类。
- `actions/executor.py`：A.6 写入顺序表；有效期在发请求线程内最后一刻检查；登记 `sending` 时 `SELECT … FOR UPDATE` 锁任务行并核对租约/工作/提案；最终结果与目标锁同事务（需待办则保留锁）；探针恰好一条序列规则；`serve` 内执行器统一入口规则。
- `config.py`：`VIGIL_VERIFY_PROBE_QUERY`、`VIGIL_WEB_HOST`、`VIGIL_WEB_PORT`、`VIGIL_WEB_DEV_ORIGIN`、审批人、授权有效期。
- `agent/tools.py`：`write_sent` 改为取 `send_state`。
- `doctor` 补探针单序列检查与 `MutatingWebhookConfiguration` 检查。

测试（A.15）：回滚一致、探针、写入不重试、原始 JSON、回读核对、写入范围、有效期、执行记录、结果一致、卡片变更清单、互斥（登记 `sending` 失败不发请求）。

完成标准：测试绿；现有 `test_actions_rollback.py` 等按新语义更新后绿，删除或改写的旧断言在提交说明中逐条列出原因。

### M4 会话运行器、审批与恢复（A.2 余下、A.5、A.6 恢复表、A.7、A.8）

改动：
- 新模块 `src/aiops/web/runner.py`（会话运行器）：领取工作、推进调查、每 60 秒续约、等待审批交接事务（存档落盘后写 `ready_proposal_id`、释放租约、工作 `done`）、会话收尾事务、启动时与每 60 秒恢复检查（A.6 表 1–7 行，内存中正在推进的调查跳过，非本实例租约视为失效）。
- 审批服务：A.5 六步单事务；拒绝置位 `actions_disabled`。
- `request_action`：页面调查只接受 `rollout_undo` 且不自动执行；`serve` 内告警调查只读；`actions_disabled` 时拒绝。
- 线索中间件：逻辑轮次、线索 → 轮次映射、成功后标记已送入、恢复与终态处理。
- 归属：`dev` 的 `approve/reject/resume/cancel/act --investigation` 遇到会话调查一律拒绝。
- `dev followup resolve`：A.6「待办收尾」事务并写会话事件。

测试（A.15）：审批、拒绝、动作范围、推进者（租约续约、交接顺序、`serve` 停止后 dev 仍不能操作页面调查）、恢复（A.6 每一行及列出的崩溃点）、并发、待办、线索。

完成标准：测试绿；恢复表每一行有一个命名清晰的测试（`test_recovery_row_<n>_...`）；页面执行在 `sending` / `sent` 后崩溃，新 `serve` 开放监听前恢复为待核实且未发出集群写请求，然后启动成功。

### M5 Web API、快照、SSE 与过滤（A.1 路由隔离、A.10–A.13）

改动：
- 依赖加入 `fastapi`（固定版本范围，更新 `uv.lock`）。
- `src/aiops/web/app.py`：独立 Web 应用对象；A.10 全部接口与错误码；Host/Origin 校验；防嵌入头；统一错误体（含覆盖 FastAPI 默认 422/404/500）；`/api` 未知路径 JSON 404；托管 `web/dist`，页面路径回退到入口，缺失时提示。
- `src/aiops/web/snapshot.py`：A.11 快照，从数据库现算，`page_state` 优先级规则。
- SSE：`Last-Event-ID` 补发。
- A.13 过滤：独立响应模型，观测摘要按工具类型重新生成，自由文本统一过滤。
- `serve`：同一事件循环中启动 Web 应用、告警应用和会话运行器；`aiops serve` 打印本机地址。
- 导出 OpenAPI 到 `web/openapi.json`（提交），提供生成脚本。

测试（A.15）：接口、防嵌入、路由隔离、快照（A/B 提案）、安全（假密钥和假路径不出现在任何响应，含模型文本与错误响应）、SSE 重连补发。

完成标准：测试绿；本地 `uv run aiops serve` 后 `curl` 验证 `/api/conversations/current`、错误码、头部和告警口 404（命令与输出写进 PR 描述）。

### M6 前端（DESIGN 第 5 节、A.1 前端、A.12）

改动：
- `web/`：React + TypeScript + Vite + Ant Design，pnpm 并提交 `pnpm-lock.yaml`，依赖固定版本；`.gitignore` 加 `web/node_modules/`、`web/dist/`。
- 类型由 `web/openapi.json` 生成并提交；提供校验"生成结果与提交一致"的脚本。
- 页面：对话（线索状态）、调查进展、证据（按假设分组）、提案卡片（变更清单、敏感值、批准/拒绝仅在 `pending_approval` 且就绪时出现、`hash_mismatch` 提示刷新）、执行与验证结果、待办、连接状态、开始新调查。
- 数据流：首次与重连先拉快照再订阅；事件合并后重拉快照；未结束或待核实时每 10 秒轮询；不使用浏览器存储作为事实来源；只按错误 `code` 判断。
- 根 `Makefile` 增加 `web-build`（`pnpm --dir web install --frozen-lockfile && pnpm --dir web build`）和 `web-check`（类型生成一致性 + `tsc`），并纳入 `check-all`；CI 工作流加 Node/pnpm 安装步骤。
- `doctor` 补页面文件检查与构建命令提示。

完成标准：`make check-all`（含前端）绿；`pnpm --dir web build` 产出 `web/dist`；`aiops serve` 下浏览器打开页面无控制台错误。

### M7 真实集群验收与文档（DESIGN 第 9 节）

- 在 k3d `vigil-lab` 上用 f01 坏发布场景，浏览器完成第 9 节全部条目：发起、进展与证据、线索"已送入（第 N 轮）"、卡片版本与变化清单、批准后真实执行与首页恢复、集群 Pod 模板与卡片一致；授权过期/目标改动/历史版本变化时"未执行"且集群未写（可用脚本人为制造）；拒绝后无新动作；刷新、断线、重启 `serve` 后一致；`aiops --help` 与 `serve` 运行时 `dev approve` 被拒。
- 截图（初始、调查中、提案卡片、批准后结果）放 `.evidence/`（不提交），运行记录（调查 ID、提案 ID、结论、命令输出）写入 `faults/f01-bad-release/runs/` 新文件，遵守该目录"保留失败、勘误不覆盖"约定。
- 更新 `README.md`、`docs/USAGE.md`、`ARCHITECTURE.md`、`GLOSSARY.md`（调查提示、会话、待核实等术语）；新增 ADR `docs/adr/0014-web-conversation.md` 记录 FastAPI 与单一推进者决策。
- 场景结束后按 f01 README 复位环境。

完成标准：第 9 节每条有对应截图或命令输出；环境已复位。

## 2. 停止条件

遇到以下任一情况，**停下并报告**，不要自行绕过：

1. DESIGN.md 某条规则在现有代码中无法按原意落地，或需要改变 DESIGN 中的接口、状态、错误码、安全承诺。
2. 为让测试变绿而需要删除或弱化 A.15 断言、跳过数据库测试、或修改与本设计无关的已有测试语义。
3. 实验集群、PostgreSQL 或模型网关不可用超过一次重试，导致 M7 无法完成；此时完成 M1–M6，M7 标为未完成并说明原因，不得伪造截图或运行记录。
4. 需要动生产环境、实验集群以外的资源、删除数据库数据（测试库内由测试自己创建并清理的数据除外），或推送 `main`、合并 PR。
5. 同一个失败连续三次修复不收敛。
6. 依赖安装需要改动系统级环境（例如升级 Node、全局安装包）。

## 3. 交付

- 所有提交推到 `origin/feat/web-conversation`，不开 PR、不合并（由主 agent 开 PR）。
- 最终报告：每个里程碑的提交 SHA、新增测试清单与 A.15 条目对照表、`make check-all` 末尾输出、M7 证据路径、未完成项与已知风险。
