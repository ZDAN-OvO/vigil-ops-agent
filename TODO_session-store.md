# 把会话、事件和执行权写入 PostgreSQL

## 目标

CLI 和 `serve` 能查询同一会话，按会话内递增的 `seq` 读事件。跨进程恢复从最近一次有效 checkpoint 继续，失去租约的旧驱动无法更新会话。

## 本期不做

- 不把旧调查迁成会话。旧数据保留查询能力。通用迁移保留旧表和租约字段，不自动作废或解锁遗留动作。遗留动作处置由动作任务交付独立操作，新轮和恢复只认会话租约。
- 对话提示、审批和告警规则由各子计划接入。

## 完成标准

- 增量 migration 能从空库和基线库反复执行。会话、轮、已排队输入、会话事件、调查和 checkpoint 具备稳定关联。主线使用会话 ID，旁路有独立 checkpoint 线程与租约。
- 告警会话保存 `group_key`，部分唯一索引约束同组最多一个未关闭会话。会话存储提供按组获取事务级咨询锁、选择未关闭会话、新建首轮和追加组消息的接口。会话存储另提供事务内关闭操作，负责锁定会话行、更新状态和追加会话关闭事件。告警服务取得组锁后发起关闭事务，动作任务在该事务内调用会话存储并作废未发送卡片。
- 同一事务持有会话行锁分配 `seq` 并提交关联状态。跨进程读取能按 `after_seq` 衔接，重复拉取可按 `session_id + seq` 去重。模型、工具和执行的内部原始数据与对外事件投影分开。
- 主线租约 120 秒，后台 30 秒续期，按数据库时间判断期限。续期前后同一代有效，过期接手才增代。主线与旁路在各自允许的范围内提交。
- 将 checkpoint 写入、待投递消息消费标记与送达事件绑定到同一事务。测试覆盖消费前崩溃、提交后回包丢失、两个进程竞争，以及新进程接手后旧进程再写 checkpoint、补写 channel writes 或追加运行事件。
- 会话任务在同一事务保护所有与当前轮相关的存储写入，包括工具请求、观测、假设、证据、预算与报告终态。真实的动作回执有单独的尝试级收尾条件，由动作任务维护。
- 用户和告警可以在不持有主线租约时并发入队。入队、旁路事件和持租约的主线状态提交使用不同的操作权限。旁路只能写自己的 checkpoint 与事件，不能提交主线状态或领取卡片。

## 关键取舍

- 保留 LangGraph checkpoint 数据格式，需要数据库写入适配器实现同连接事务和代次校验。先证明依赖的 checkpoint 保存与 pending writes 两条路径都能受保护，再接入真实 Agent。仅在调用前查一次租约会留下提交竞态。

## 前置依赖

- 无。

## 已核实事实

- `src/aiops/db.py:69`、`:97` 发现并执行迁移，`:1090` 的旧租约以调查 ID 为单位。
- `src/aiops/agent/engine.py:212` 使用 `AsyncPostgresSaver`，`:301` 绑定调查 ID，`:340` 领取 300 秒旧租约。
- `tests/test_docs.py:19` 同时收集跟踪与未跟踪文档，`:88` 的退役规则误判当前计划链接。
- `tests/test_db.py:133` 和 `:142` 覆盖重复执行和空库迁移。真实库跳过条件在 `:45`。

## 变更范围

- 新增 `src/aiops/sessions/` 的会话模型、消息入队、事件读取、租约和 checkpoint 适配。
- 在 `tests/test_docs.py` 先调整退役引用规则，允许根目录 `PLAN.md` 和 `TODO_*.md` 互链和引用现行 `DESIGN.md`。保留所有文档的相对链接和锚点检查，继续检查正式文档是否引用临时计划以及所有文档是否引用其它已删除资源。
- 在 `src/aiops/migrations/` 追加会话迁移，复用 `Store` 的连接和迁移逻辑。会话任务提供带持有者与代次的事务入口，改造 `db.py` 的调查账本写入方法。新会话路径按此写入，旧调查沿只读路径访问。
- 与动作任务对齐锁顺序。领取、关闭和取得发送权都先锁会话行，再操作对应动作记录。多条记录按稳定顺序加锁。

## 风险与回滚

- 租约检查和保存分成两个事务会被过期驱动写穿。用库内条件、同事务锁和两个连接的竞争测试挡住。
- 启用新入口前用基线数据库的脱敏副本演练。回退时保持新增表和数据可读，旧驱动不得消费会话队列。

## 验证

- `uv run --frozen pytest -q tests/test_db.py tests/test_agent_recovery.py`，使用独立 PostgreSQL 测试库。追加本任务的双进程、事务、代次和迁移回归。
- 两条数据库连接同时尝试为同一 `group_key` 创建首轮，最终只保留一个会话和一条待跑首轮。运行 `uv run --frozen pytest -q tests/test_docs.py`，确认保留计划文件时通过门禁。
- 集成门禁运行 `make check-all`，核对以上数据库用例实际执行。

## 停止条件

- 依赖无法在同一事务提交受代次保护的 checkpoint 和投递结果时，先收敛存储适配方案。
- 目标库存在未核实的旧动作时，停在正式切换前。临时测试库仍可验证迁移。

## 进度

- [x] 文档门禁、迁移和事件契约
- [x] 入队、租约与 checkpoint 事务
- [x] 跨进程验证

## 范围外的问题

旧调查的全文检索留在后续任务。

## 下游接入契约

以下签名位于 `aiops.sessions.store`、`aiops.sessions.models` 和
`aiops.sessions.checkpoint`。事务组合接口在调用方成功退出上下文后提交。
通过 `Store.migrate() -> list[str]` 升级业务表，再调用
`setup_checkpoints(store: Store) -> None` 升级 LangGraph 官方存储。
后者需要并发建索引，单独在启动时运行。

### 读取和发消息

- `SessionStore(store: Store)`
- `create_session(*, title: str) -> Session`
- `get_session(session_id: str) -> Session | None`
- `list_sessions(*, limit: int = 50) -> list[Session]`
- `get_turn(turn_id: str) -> Turn | None`
- `list_turns(session_id: str) -> list[Turn]`
- `list_runnable_main(*, limit: int = 50) -> list[str]`
- `list_runnable_side(*, limit: int = 50) -> list[str]`
- `enqueue_input(session_id: str, text: str, *, display_text: str, input_id: str | None = None) -> SessionInput`
- `get_input(input_id: str) -> SessionInput | None`
- `list_events(session_id: str, *, after_seq: int = 0, limit: int = 100) -> list[SessionEvent]`

`text` 是队列原文。客户端只读取事件，调用方写入事件前要提供脱敏的标题、`display_text`
和出站 payload。以 `SessionEvent.as_dict()` 得到 `type`、`session_id`、`seq`、`ts`、
`turn_id` 和事件类型字段。`input_delivered` 带 `input_id`、`step`、`lane`。
同一会话用同一输入 ID 重试相同文本时返回原记录。队列扫描只找无有效主租约且可运行或可接手的普通轮次。
等待已决卡片的扫描由处置模块补上。

### 驱动与调查账本

- `acquire_main_lease(session_id: str, owner: str) -> Lease | None`
- `acquire_side_lease(session_id: str, owner: str) -> Lease | None`
- `renew_lease(lease: Lease) -> Lease`
- `release_lease(lease: Lease) -> bool`
- `start_main_turn(lease: Lease) -> Turn | None`
- `pending_inputs(lease: Lease, turn_id: str, *, limit: int = 100) -> list[SessionInput]`
- `async with sessions.run(lease: Lease, turn_id: str) as tx` 提供 `RunTransaction`
- `tx.append_event(type: str, payload: Mapping[str, Any]) -> SessionEvent`
- `tx.pending_inputs(*, limit: int = 100) -> list[SessionInput]`
- `tx.set_turn_state(state: TurnState) -> Turn`
- `store.for_turn(lease: Lease, turn_id: str) -> Store`
- `store.get_investigation_id_for_turn(turn_id: str) -> str | None`
- `SessionCheckpointer(store: Store, lease: Lease, turn: Turn)` 兼容异步 LangGraph saver
- `aget_tuple(config: RunnableConfig) -> CheckpointTuple | None`
- `alist(config: RunnableConfig | None, *, filter: dict[str, Any] | None = None, before: RunnableConfig | None = None, limit: int | None = None) -> AsyncIterator[CheckpointTuple]`
- `aget_delta_channel_history(*, config: RunnableConfig, channels: Sequence[str]) -> Mapping[str, DeltaChannelHistory]`
- `aput(config: RunnableConfig, checkpoint: Checkpoint, metadata: CheckpointMetadata, new_versions: ChannelVersions) -> RunnableConfig`
- `aput_writes(config: RunnableConfig, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = "") -> None`
- `get_next_version(current: str | None, channel: None) -> str`

`owner` 为驱动实例的唯一标识。驱动每 30 秒续期，期限为数据库时间 120 秒。
用 `Lease` 的持有者与代次比较权限，`expires_at` 供显示。存储拒绝失去有效租约的写入并抛出
`LeaseLost`。关闭的会话拒绝入队和领取。主线接手返回原轮，暂停轮显式转回 `running` 后才能
送达新输入。旁路领取为最早一个待投递输入创建单独的轮和租约，已启动的旁路可在主线继续后
完成自己的轮。主线与旁路的账本写入均限定在其自身轮，旁路不能进入动作或审批阶段。

使用 `store.for_turn` 提供给复用现有账本 API 的工具和运行时。新增调查由该 scoped store 创建，
一轮最多一个调查。组合写入时串行使用 `tx.ledger`，在同一连接按顺序完成账本写入、事件、轮次状态
切换，退出上下文提交。终态写入最后改变轮状态。现有历史查询接口继续可用。

根 checkpoint 线程为 `turn.checkpoint_thread_id`，根的 `checkpoint_ns` 是空字符串。
保存器将实际轮 ID 写入 checkpoint metadata 的 `vigil_turn_id`，字段名由
`TURN_ID_METADATA` 导出。主线可按它区分本轮恢复点与上一轮上下文。
接入 LangGraph 时用 `durability="sync"`。把排队输入加入图状态时，在同一根状态更新中写入普通
`vigil_input_ids: list[str]` 状态通道，图的消息输入也使用普通非 Delta 通道。
`INPUT_IDS_CHANNEL` 导出该字段名。保存器按当前轮可消费的 FIFO 前缀核对 ID，用 LangGraph
metadata 的 `step` 记录送达位置。仅根 checkpoint 可提交送达。按原 checkpoint ID 重试不会
重复写事件。普通 pending writes 走同一租约栅栏。

### 告警与处置

- `async with store.transaction() as conn` 提供 `AsyncConnection`
- `async with sessions.alert_transaction(group_key: str) as conn` 在同一连接先持事务级组锁
- `get_open_alert_session(conn: AsyncConnection, group_key: str) -> Session | None`
- `record_alert(conn: AsyncConnection, group_key: str, *, title: str, text: str, display_text: str, repeat_display_text: str) -> AlertReceipt`
- `append_alert_message(conn: AsyncConnection, session_id: str, *, display_text: str) -> SessionEvent`
- `close_session(conn: AsyncConnection, session_id: str, *, reason: str) -> SessionEvent | None`
- `lock_session(conn: AsyncConnection, session_id: str) -> Session`
- `require_lease(conn: AsyncConnection, lease: Lease, *, main_only: bool = False) -> Session`
- `require_turn(conn: AsyncConnection, lease: Lease, turn_id: str) -> Turn`
- `append_event(conn: AsyncConnection, session_id: str, type: str, payload: Mapping[str, Any], *, turn_id: str | None = None, allow_closed: bool = False) -> SessionEvent`

告警操作先拿组锁，再读或锁会话。持锁调用 `record_alert`，首次返回待跑轮 ID，重复推送只追加
组消息。关闭返回关闭事件，重复调用返回 `None`。处置在同一个连接上先锁会话、再按稳定顺序
锁动作记录，组合关闭、作废、解锁等操作。取得发送权使用 `require_lease(..., main_only=True)`，
更新发送权之前再次检查期限。运行事件统一走 `run()`。结构化授权与尝试回执可以在各自受保护的
业务事务直接追加事件，已取得发送权的尝试可用 `allow_closed=True` 记录回执。
