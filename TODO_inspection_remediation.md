# 核对巡检异常并按登记策略处置

## 目标

任一轮发现明确异常时，由一个 Agent 主线核对已有观察和所需的补充证据。命中巡检处置规则后，Vigil 只对持续异常目标发送一次原动作，并记录验证、失败及必要的补偿结果。

## 本期不做

- 节点和入口告警迹象只作为调查依据。自动动作限于已登记工作负载。
- 活动故障上的额外动作转人工。同一目标的观察全部保留。

## 完成标准

- 有异常时一轮至多创建一条巡检主线，来源为 `scheduled`，加载该轮 SOP 版本并关联各异常目标、观察和规则结果。Agent 核对结果入账。缺模型、调查失败或证据不齐时保留原因与人工待办。
- 只有显式启用巡检与既有自动处置、策略合法、专用写身份和准入校验通过、Agent 已核对且规则得到可验证证据时，才能产生规则提案。动作及参数来自规则，缺任一条件时仅记录结论。
- 同一工作负载多个规则命中、后续轮次继续异常、告警与巡检竞争、或进程重启，均共享同一个活动故障与原动作发送名额。同一故障的补偿受既有独立的一次名额约束。
- 每轮采集完成后，健康轮也主动评估现有巡检故障的恢复条件。健康结案后再发可获得新的故障记录。若仍异常、证据不足或写入结果未知，原故障和已用名额保持有效。

## 关键取舍

- YAML 策略使用 `type: alert|inspection`，旧规则省略时视为 `alert`。巡检规则声明检查标识、所需观测、现场值条件、新鲜度、目标参数和优先级，使用巡检观察匹配。优先级按目标唯一，冲突配置校验失败。多条满足时只选择优先级最高的一条，其他结果入摘要。
- 按集群、命名空间和实际工作负载 UID 聚合，多条规则共用 `remediation_faults` 和发送事务。告警与巡检各自保存证据，可关联同一故障。工作负载同名重建时，未结的原 UID 故障或未核实写入继续阻止该登记目标获得新自动名额，写入身份始终核对实际 UID。
- 巡检主线预算取现有调查预算与本轮剩余时间的较小值，扣除已知的核对、发送预检及反馈所需预留。持久化本轮期限，截止即取消巡检主线并让尚未取得发送权的原动作失效。已取得发送权的动作由原账本继续核实、观察和执行既有补偿。普通调查的预算沿用现值。

## 前置依赖

- [调度和 SOP](TODO_inspection_run.md) 提供巡检会话、可引用观察、SOP 版本以及可在事务中核对的轮次期限和持有权。

## 已核实事实

- `src/aiops/contracts.py:27` 已接受 `scheduled` 事件来源，`src/aiops/agent/conversation.py:156` 当前只按人工或告警建立事件。
- `src/aiops/actions/remediation_policy.py:221`、`src/aiops/actions/policy.py:124` 的匹配和授权目前依赖告警名称及标签。
- `src/aiops/migrations/0016_remediation.sql:37` 按工作负载 UID 约束活动故障，同文件第 105 行约束原动作和补偿发送名额。
- `src/aiops/webhook.py:113` 依次取告警组和目标名称锁，`src/aiops/actions/remediation_runtime.py:635` 在目标和会话锁后以 UID 锁保护故障读取，`src/aiops/actions/remediation_store.py:296` 在发送事务中复查授权、会话、故障和名额。
- `src/aiops/actions/remediation_runtime.py:1037` 负责业务观察，`src/aiops/actions/remediation_runtime.py:1223` 生成补偿，`src/aiops/actions/remediation_alerts.py:184` 核对告警恢复和历史动作。
- `src/aiops/actions/remediation_policy.py:507` 计算策略版本，`scripts/generate_remediation_manifests.py:391` 生成绑定该版本的集群准入。

## 变更范围

- `src/aiops/agent/conversation.py`、`src/aiops/agent/engine.py` 与巡检适配入口将轮初观察交给 `scheduled` 主线分流。该轮工具限制为登记范围内只读取证，核对回执绑定候选、目标和证据 ID。授权所需的样本不够新鲜时，由程序在剩余预算内按登记检查重采，并交主线核对更新后的证据。缺少有效回执或证据则转人工。发送事务再次验证来源、现值和年龄。
- `src/aiops/actions/remediation_policy.py`、`src/aiops/actions/policy.py` 与 `config/remediation/` 增加巡检规则及隔离试验配置。保持纯告警旧策略的规范化版本摘要兼容。启用新规则时，用该策略目录生成并应用对应版本的准入清单。换策略前先排空在途动作或核清未知写入。
- `src/aiops/actions/remediation_runtime.py`、`src/aiops/actions/remediation_alerts.py`、`src/aiops/actions/remediation_store.py` 和追加迁移接入巡检信号。发送权按来源分派检查，复核目标 UID、现值、策略、证据和轮次。结轮只更新轮次与会话，待发送动作由处置账本核对轮次后作废。原动作、观察和补偿读带来源的持久证据，旧任务继续读原格式。
- 建立和关闭故障共用一套加锁顺序。告警组锁（哈希种子 0，如需）→ 目标名称锁（种子 2）→ 会话及相关记录（如需）→ 故障 UID 锁（种子 1）→ 故障行。巡检健康结案从目标名称锁进入，在该锁下读取告警状态，告警会话和告警行仍由原有告警流程写入。取齐锁后再读取最近结案时间、关联信号和未定写入。早于结案的迟到信号不能重开故障。
- 处置完成和后续每轮有效采集都可触发结案。`RemediationStore.finish()` 的结案分支也须按上述顺序取得故障 UID 锁与故障行锁，再复查并写结案时间。核对业务探针观察窗，以及晚于最后异常的全套必要健康检查。混合故障还须满足既有告警恢复条件。异常记录过期、消失或被移出配置不会自动结案。
- 复用已有写身份、准入、动作锁、审计、补偿与人工核实链路。到期和授予发送权竞争时，以数据库事务顺序和期限核对为准，失效轮的结果不能通过晚到回执发起新动作。

## 风险与回滚

- 目标级合并会让并发的第二个真实问题暂时没有自动名额，剩余问题和证据可在会话查看。
- 规则或 SOP 版本变化使旧候选需要重新核对。已经获得发送权、结果可能未知时保留动作锁与核实记录。
- 关停后不再授予新巡检原动作发送权。处置账本继续核实已发送任务。版本回退保留现有故障、发送记录和未解锁状态。

## 验证

- 快测新增 `tests/test_inspection_remediation.py` 后执行 `uv run --frozen pytest -q tests/test_inspection_remediation.py tests/test_actions_policy.py tests/test_remediation_observation.py`。
- 同一环境配置独占数据库后运行 `uv run --frozen pytest -q tests/test_inspection_remediation.py tests/test_remediation_store.py tests/test_remediation_fault_clearance.py tests/test_remediation_integration.py`。用并发屏障覆盖多规则、跨入口、发送权前后超时和崩溃、陈旧证据、健康结案后复发、未恢复继续限频及恶化补偿。统计发送权和外部写调用次数。
- 在 `tests/test_remediation_fault_clearance.py` 用双事务屏障覆盖健康结案先提交、同目标告警先提交两种场景。两条路径均按约定顺序加锁。断言无死锁、活跃告警阻止结案、早于结案的迟到告警拿不到新名额，以及真实复发只获得一个新名额。另用第二条数据库连接探测 UID 锁，分别断言健康结案和处置完成结案提交前均持有该锁，避免名称锁掩盖遗漏。
- 合入前执行总纲 Linux 门禁与真实环境核对。

## 停止条件

- 需要独立的自动写入口、突破已有动作准入，或不能保留原有告警处置和恢复行为。
- 自动处置可以在重启、跨入口或新规则重复命中后绕过同一故障名额。

## 进度

- [ ] 形成巡检证据、Agent 核对与登记规则契约。
- [ ] 从两类输入共用目标锁、故障账本、发送权和结案检查。
- [ ] 跑通写入、观察、补偿与并发回归。

## 范围外的问题

- 跨工作负载识别共同根因及自动联合处置。
