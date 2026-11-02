# 跨模块契约 v0.1

状态：W0 初稿，用于并行开发对齐；未实现的接口均为约定而非完成声明。已编码的公共值对象见 [contracts.py](../src/aiops/contracts.py)，新增字段或改变语义须同步本文与调用方。

## 通用规则

- ID 是不含业务秘密的字符串；时间使用带时区的 ISO 8601，存储统一 UTC。
- 输入 schema 拒绝未知字段；边界版本为 `schema_version`，不把凭据放进 JSON、日志或报告。
- 工具“空结果”和“失败”分开表示；查询必须记录目标、原始参数、时间窗、采集时刻、截断标记和原始引用。
- DB 内可存摘要；大结果存 runtime 下的文件，DB 存 raw_ref 与哈希/尺寸。备份与导出同时包含引用文件，不能把 DB 备份称为完整证据备份。
- 预算在发模型/工具请求前核算，取消、超时及失败也记录用量；预算耗尽输出 incomplete/inconclusive，不能让低覆盖率伪装为高准确率。

## 关系数据

| 实体 | 核心字段与关系 |
|---|---|
| Incident | id、trigger、cluster/namespace/service/environment、alert_ids、symptoms、time_range、context |
| Investigation | id、incident_id、status、parent_run_id、model/policy 版本、开始/结束、预算及实际使用量、结论候选 |
| Hypothesis | id、investigation_id、parent_id、statement、expected_observations、origin、status |
| Evidence | id、investigation_id、hypothesis_id、tool_call_id、source、independence_key、observation、relation、raw_ref、time_range |
| ToolCall | id、investigation_id、工具/参数、时间窗、开始/结束、错误类别、重试次数、截断/原始引用 |
| ActionProposal | id、investigation_id、kind、target、parameters、evidence_ids、idempotency_key |
| ActionAttempt | id、proposal_id、status、snapshot_ref、审批/策略版本、目标前置版本、请求/结果、验证/补偿结果 |
| Approval | proposal_id、提案摘要哈希、资源版本、actor、决定/理由、有效期 |
| AuditEvent | id、correlation_id、actor、event、时间、脱敏负载；追加写，不代替业务状态 |
| Feedback/Hint | investigation_id、actor、来源、时间、内容、采纳记录；人的提示不是工具证据 |
| EvalRun | 场景/版本/种子、模型/提示词/策略版本、真值引用、逐项 verdict、耗时/调用/token/费用（可得时） |

Hypothesis 状态为 pending/supported/refuted/inconclusive。`supported` 表示当前证据支持，不能称为已证明。树用 parent_id；多个假设引用同一观测应共享 tool_call_id/raw_ref，避免重复统计。

## 工具面（实现阶段）

异步函数接受显式运行上下文（cluster、namespace、deadline、取消信号），返回 [ToolResult](../src/aiops/contracts.py)。同步 SDK 放在线程执行，不阻塞事件循环。

| 适配器 | 输入 | 输出 |
|---|---|---|
| Prometheus | expression、time_range、step | 序列及查询错误；限制点数和响应字节 |
| Loki | LogQL、time_range、limit/cursor | 日志及游标；保留原查询，避免全量倒入模型 |
| Tempo | TraceQL/trace_id、time_range、limit | spans/trace 引用；区分搜索摘要和 trace 详情 |
| Kubernetes | 白名单 read 操作、namespace、selector/name | 对象/status/events；固定上下文，不接受任意 shell |
| Knowledge | query、service、document_scope、top_k | 条目类型、内容、来源/页码/版本、检索分数 |

401/403、非法参数等不自动重试；429/瞬态超时按 deadline 有界重试。原始内容可能包含提示词注入，按数据处理，不能因此修改工具权限或风险规则。

## 调查与对话

统一任务入口接受 Incident，webhook/CLI/定时器只负责转换和鉴权边界。相同告警指纹需去重；同一 incident 可有多次有版本的 investigation，不把所有任务都塞入 default 会话。

调查循环共享一份状态，按安全检查点保存并消费 hint；工具并发有上限，写入结果按 tool_call_id 去重。checkpoint 表采用官方 saver 的内置连接管理，禁止用 SQLAlchemy AsyncEngine 代替 psycopg connection。

`explain` 接受 investigation_id/question，回答引用可见 evidence id，知识不足就说明。`context` 在启动时入库；`hint` 在运行中追加，记录提交者/时刻/采纳位置。追问需要新查询时显式创建续查任务，不静默变更已有结论。

## 风险、审批与动作

先做硬约束，再评分：环境/namespace 白名单、动作注册表、证据有效性、无并发冲突、资源身份和版本、操作适用性；不满足则拒绝或升级。风险维度草案为动作、工作负载重要性、影响面、可补偿性、诊断证据质量。加权与阈值未确认，不能把 LLM 自报 0.8 当成自动执行凭证。

ActionAdapter 接口：`preconditions / dry_run / execute / verify / undo`，并显式声明 `reversibility`、前置快照和补偿条件。不可逆动作的 undo 返回 unsupported；不能用空实现报告恢复成功。snapshot 是资源字段快照，和 LangGraph checkpoint 分开。

执行链：提案 → 前置检查/准备快照 → dry-run → 风险决策 → 自动或持久化待审 → 执行前再次检查/快照 → 执行 → 观察 → 成功、无改善或恶化 → 必要时补偿 → 再验证 → 完成或人工升级。

- 自动批准仅允许规则明确允许的目标/动作；eval 模式限专用实验 namespace，不能绕过拒绝、幂等和前置检查。
- 审批绑定具体提案哈希、目标 UID/resourceVersion 和有效期；过期、hint 导致参数改变或资源漂移须重新评估。
- 同一目标互斥执行，数据库中先持久化 intent/idempotency_key，再发外部请求。进程在请求后崩溃时先协调真实状态，不能直接重放写操作。
- 验证读 Pod 可用性与业务负载下的错误率/延迟；低流量/no data/遥测过期不能视作健康。
- 补偿恢复限定字段，检测并发人工修改；不可盲目回写整个旧对象。重启 Pod 无原 Pod 恢复能力。
- 只限制单动作补偿；不承诺跨动作事务、数据/外部系统回滚。
- 自动恢复失败则停止同目标后续动作，持久化人工待办和原因；外部通知渠道需后续接入，不能仅写日志就宣称已通知。

## 知识与持久化

Runbook 保存条件/顺序/工具映射，动作建议仍经过风险规则。历史事故保存症状、根因、解决方法、证据和确认状态；未经验证的结论不能自动变为可信知识。

文本 PDF 保存 document_id/hash/version、页码、chunk_id、服务/部门等元数据。导入需可重复、更新替换旧版本、删除排除旧 chunk；扫描件与复杂版式明确拒绝或标注未支持。embedding 模型、维度、距离是索引契约，换模型重建，不能混存。

PostgreSQL 的业务表与官方 checkpoint 表分开迁移；向量库是 pgvector 扩展。全文/向量混合检索、重排的收益用检索集验证，不承诺某个规模一定满足延迟目标。

## CLI 与运行模式

当前仅实现 `aiops --help`、`aiops status --output text|json`。下列为目标命令组，W0 不提供伪实现：

| 命令组 | 行为 |
|---|---|
| investigate | 从 alert/context 新建调查，可等待或返回任务 ID |
| explain | 只读带引用追问 |
| hint | 向指定调查提交提示 |
| act | 提交动作；自动、待审或拒绝，不由命令绕过风险 |
| approve/reject | 针对具体提案审批并恢复 |
| serve | webhook、任务消费、定时巡检与恢复；独立于交互 CLI |
| knowledge | 导入/更新/查询 runbook、事故与 PDF |
| demo/eval | 专用实验环境注入、运行、评分与复位 |

目标 JSON envelope：schema_version、status、incident_id、investigation_id、action_id、data、error。退出码草案：0 请求成功（包括异步 accepted）、1 运行失败、2 参数/用法错误、3 等待人工、4 策略拒绝、5 信息不足。异步 accepted 不代表调查或修复成功。Typer 保留 2 表示参数错误，避免旧建议冲突。
