# 用同一个 Agent 连续对话并恢复被中断的调查

## 目标

用户可以连续问集群问题，在调查中发线索，也可以在待审批时进行只读问答。进程重启后从最近一次持久化步骤继续，用户可查上轮结果和本轮处理进度。

## 本期不做

- 不增加写动作或把聊天文本当审批。审批结果来自卡片授权事务。
- 不增加跨会话检索，不用上一轮缓存回答当前状态。

## 完成标准

- `LangChainInvestigation` 的运行和恢复由会话租约取代旧 300 秒调查租约。历史调查只读路径无需领取租约。更新现有租约冲突用例，让两个试图驱动同一会话的进程竞争同一代次。
- 简单问答可以直接回答。故障排查仍创建结构化报告，并复用证据与结论校验。假设和带时间的证据跨轮可读。两轮之间改变副本数后，本轮重新查询。
- 模型调用前按入队顺序送入线索，送达步骤来自已提交 checkpoint。一条新输入若赶不上本轮收尾，就成为下一轮首条输入，后续输入继续按队列处理。暂停分支把未送达输入交给旁路，用户能分辨线索是否送进主线。
- 先保存主线暂停 checkpoint，再发布可审批卡片。两步之间崩溃后，接手恢复能补发一次。旁路只读引入暂停点的消息与证据摘要，按轮使用独立预算，禁止提卡片和更新主线。审批到达时已经运行的旁路可以答完，已完成的旁路消息只作为后续主线线索，不覆盖原卡片。
- 主线按已持久化的决定恢复原中断点。审批停留期间保留已消耗预算，旁路的预算不计入主线。预算耗尽按设计返回不完整结果。
- 停止接收新调用后，已记录结果的查询不重放。记录缺失的查询可以重做。查询复用键限定在本轮，写结果来自处置回执。关闭或丢失租约后不能发起新的模型与工具调用。

## 关键取舍

- 保留现有 Agent 和调查报告，允许每轮选择直接回答或展开调查。旁路用同一 Agent 的只读工具集合和单独线程，避免审批等待把整个会话冻住。
- 由本任务与动作任务先固定卡片发布、已决卡片读取、恢复结果三处接口。定义主线从已批准或已拒绝状态恢复时消费的唯一决定 ID。动作任务拥有授权和发送事务，本任务拥有 checkpoint 与继续调用。

## 前置依赖

- [会话账本与租约](TODO_session-store.md) 提供队列、租约、受保护的 checkpoint 和事件。

## 已核实事实

- `src/aiops/agent/engine.py:225` 创建现有 Agent，`:352` 与 `:397` 是调查和审批恢复入口。
- `src/aiops/agent/middleware.py:74` 计量预算，`:156` 限制动作调用批次。
- `src/aiops/agent/tools.py:225` 执行只读工具，`:326` 查重复调用，`:1288` 调用 `interrupt`，`:1291` 恢复动作。
- `tests/test_agent_recovery.py:61` 覆盖旧任务租约冲突，`:174` 检查已保存的动作回执。

## 变更范围

- 在 `src/aiops/agent/` 接入会话轮次、轮内投递、恢复、只读旁路和卡片回执。将工具请求、观测、假设与证据、用量和报告收尾接到带租约代次的存储入口。
- 调查工具保留现有证据账本。会话事件引用对应观测、假设、证据和卡片记录，避免在客户端重建关系。

## 风险与回滚

- 投递完成后再补写 checkpoint 会造成丢失或重复。由存储层事务解决，在暂停、预算停止和正常收尾切换时验证。
- 导入新对话入口后仍保留旧报告只读查询。入口回退时停止新轮次，并留存待消费消息和已完成的报告。

## 验证

- `uv run --frozen pytest -q tests/test_agent_flow.py tests/test_agent_recovery.py tests/test_agent_budget.py tests/test_agent_actions.py tests/test_runtime_boundaries.py`。
- 使用独立 PostgreSQL 与可停在模型、工具或卡片边界的替身，验证主线、旁路、重启、预算、关闭和超过 300 秒的持租约流程。让旧驱动的工具结果和报告收尾在新驱动接手后才返回，确认均未入库，接手者可继续。按 [总纲](PLAN.md#门禁) 做集成检查。

## 停止条件

- 无法在仍有旧的模型或工具调用在途时阻止它继续提交，暂停入口联调。
- 暂停恢复导致同一决定二次生效、回执丢失或已完成查询重做，保留现场并查对应事务。

## 进度

- [x] 轮次和插话
- [x] 暂停、旁路和恢复
- [x] 多驱动竞争和故障注入

## 范围外的问题

跨会话推荐和复盘不在本期。

## CLI 与 serve 接入

从 `aiops.agent.conversation` 导入 `ConversationDriver`、`ConversationResult` 与
`AwaitingActionReceipt`。启动时先用 `Store.migrate()` 升级业务库，再用
`aiops.sessions.checkpoint.setup_checkpoints(store)` 升级 LangGraph 表。

- `ConversationDriver(settings=..., store=..., toolbox=..., executor_factory=..., model=..., project_text=...)`。
  工具箱使用现有 `Toolbox`。生产环境按 Settings 创建模型，测试可注入模型和执行器替身。
  入口传动作任务的共享文本投影；独立运行时可使用本模块的 `redact_text`。
  消息用 `driver.sessions.enqueue_input(session_id, text, display_text=..., input_id=...)`，
  会话标题与 `display_text` 要先投影。客户端只需要读会话与事件。
- `async with driver.claim(session_id, lane="main") as run` 领取主线，`lane="side"`
  领取一个旁路轮。`run is None` 表示该 lane 暂无可领取的驱动权或输入。
  等待期间保持上下文会独立续租，退出时释放。一次性模式在退出前关闭上下文；
  交互入口可持有主线并在卡片处再次调用 `drive()`，读取跨进程提交的决定。
- `await run.drive() -> ConversationResult | None` 驱动一轮到停止点。
  `None` 表示已领取但没有工作。结果的 `result` 是持久化 `SessionEvent`，
  `exit_code` 从事件读取，`card` 为主线此次暂停的 `approval_required` 事件或 `None`。
  旁路始于待审批状态，成功或预算停止时退出码为 3，结果只属于旁路轮。
  驱动丢租约时抛 `LeaseLost`，关闭的会话拒绝开始新驱动；发现已开始且仍未收尾的动作尝试时抛
  `AwaitingActionReceipt`，动作任务收敛尝试后继续驱动。存储错误交由入口映射退出码 1 或调度重试。
- `serve` 合并 `list_runnable_main()`、`list_runnable_side()` 和动作模块的
  `list_runnable_card_sessions()` 扫描结果，实际运行前仍领取相应租约。若会话停在卡片上，
  CLI 可独立领取旁路，保持现有主线持有者继续轮询决定。
- 事件用 `SessionStore.list_events(session_id, after_seq=...)` 和 `SessionEvent.as_dict()`。
  单次机器输出以返回的 `result.seq` 为本次上界，入口处理续读与末行 `result`。
  `tool_call` 的稳定 ID 与 `turn_id` 一起关联请求和结果，字段包括 `tool`、
  `status`、`summary`、`observation_id`，查询完成时可带 `collected_at` 与 `time_range`。
  `hypothesis` 带 `id`、`statement`、`status`、`parent_id`，`evidence` 带
  `observation_id`、`hypothesis_id`、`relation`。
  `message` 的助手事件带 `lane`。`result` 带 `status`、`summary`、`exit_code`、
  `schema_version=1`、`lane`、`investigation_id`，完成时还有 `termination_code`，
  停在卡片时带 `card_id`。原始结果和文件引用在调查账本内部，事件只引用观测 ID。
  `input_delivered` 由 saver 在保存根 checkpoint 的同一事务提交。
- 告警首轮由账本附带首条告警输入。首次排查的集群和命名空间来自 Settings，查询工具仍在固定范围内。
  当前告警账本只保存首条文本，具体 `alert_names` 的结构化传递由服务入口联调补齐。
  旁路创建时按数据库创建时间找到最近的已发布卡片，并读取卡片之前的消息及该调查已发布的观测引用。
  旁路完成后，只在后续主线到达模型边界时将问答作为线索补入。现有账本接口未直接保存
  旁路对应的暂停序号；如需跨数据库时钟调整保持同一边界，应由旁路领取事务保存该序号。

## 动作接入

- `executor_factory(*, lease, turn_id)` 返回执行器的异步上下文管理器。只在持有效主线租约时调用。
  生产装配把 `lease`、`turn_id` 传作 `ActionExecutor(session_lease=..., session_turn_id=..., ...)`。
  缺少会话卡片接口时拒绝动作准备、发布与执行。
- `publish_card(proposal_id, *, lease, turn_id) -> SessionEvent` 在根 checkpoint 和
  `__interrupt__` pending write 都保存后调用。需要按提案幂等并返回同一条事件。持久化中断后
  发布失败时，本轮仍可进入普通主线接手扫描；发布成功后写等待结果并推进轮次状态。
- `get_card_decision(proposal_id, *, session_id) -> Approval | None` 返回授权事务提交的决定，
  `Approval.id` 随 `Command(resume=...)` 进入原中断点，工具在消费前复核对应持久化决定。
  无审批记录的已决提案或尝试用持久化记录标识恢复。已有终态尝试直接回放回执；
  `intent` 与 `executing` 交给处置模块收敛。执行由 `execute_authorized(proposal_id, actor=...)`
  和处置模块的发送权事务完成，`card_decision`、`action_result` 由处置模块追加。
- 准备动作落库后、工具账本还未关联提案时如果驱动中断，接手者只对可唯一核对的同轮草稿调用
  受保护的 `abandon`。处置事务撤销未发送授权并释放幂等键，之后才结束本次工具请求。
  已有关联的准备结果从工具账本恢复，自动授权也在持久化关联之后才进入执行器。

现行旧 webhook 与历史调用仍使用原调查入口，等入口任务切换后退役。
会话入口不领取旧调查租约，历史调查只读查询继续由 Store 提供。
