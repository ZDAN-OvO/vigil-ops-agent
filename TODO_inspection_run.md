# 按 SOP 定时采集并记录每一轮巡检

## 目标

由 `vigil serve` 在后台按时创建巡检会话，按 SOP 主动调用只读工具，保存状态、原因与摘要。服务没有业务异常时，值班人员也能查到巡检确实进行了哪些检查。

## 本期不做

- 调度由 `serve` 管理。到期跳过的时间点不补跑。
- 节点和入口基线仅生成巡检观察和调查证据。

## 完成标准

- 缺省配置下没有巡检会话或巡检工具调用。启用后第一轮在间隔到期时开始。同一数据库和集群即使启动两个进程，每个时点也最多启动一轮。
- 检查带稳定标识、目标、SOP 版本、采样时间、结果和证据引用。状态包括健康、异常、信息不足。信息不足保留采集失败、无数据、截断、过期或超时等原因。
- 一项检查失败后继续其余检查。单轮截止时停止采集和主线调查，保留已完成项，未完成项注明超时。恢复进程把因退出失去持有权的运行轮次结为中断，保留已写入记录。
- 每个轮次提供集群、会话、触发时点、期限、完成状态、检查结果与证据引用。观察完成后可提交有异常的主线调查，正常轮没有模型请求。

## 关键取舍

- 沿用 `Settings` 的环境变量入口。新增 `VIGIL_INSPECTION_ENABLED` 默认 false、`VIGIL_INSPECTION_INTERVAL_SECONDS` 默认 900、`VIGIL_INSPECTION_TIMEOUT_SECONDS` 默认 600、`VIGIL_INSPECTION_CONFIG` 指向版本化的 YAML 巡检清单。启用时配置缺失或不合法，保留现有服务入口并明确记录巡检无法启动的原因。
- 本地巡检 Skill 用结构化元数据声明步骤 ID 及顺序，正文指导异常调查。通过 `Settings.skills_path` 读取当前配置目录或内置目录，以同一份经过校验的内容绑定预注册只读调用并计算哈希。运行中使用该版本。若异常主线加载不到同一版本，则记录失败并转人工。
- `VIGIL_INSPECTION_CONFIG` 是只读巡检范围的登记来源，包含集群、工作负载（命名空间、类型、名称）、预期节点名、预期入口探针标识，以及各检查的参数、阈值和最大样本年龄。服务检查就绪状态与登记的业务信号，节点检查 Ready，入口检查黑盒探针成功状态。观测中缺失的登记目标记为信息不足。处置策略与写凭据仅影响写入。

## 前置依赖

- 无。

## 已核实事实

- `src/aiops/serve.py:119` 负责监听和后台任务，`src/aiops/serve.py:188` 启动时迁移并装配只读与处置依赖。
- `src/aiops/config.py:22` 读取 `VIGIL_` 配置，`src/aiops/investigation/factory.py:59` 装配 Kubernetes 和 Prometheus 只读客户端。
- `src/aiops/monitoring/signals.py:17` 定义入口探针，`src/aiops/monitoring/signals.py:233` 读取节点状态。现有展示结果不能直接充当带原始时间戳的处置证据。
- `src/aiops/skills.py:89` 解析 Skill 并计算内容哈希，`pyproject.toml:41` 将 Skill 打入 wheel。
- `src/aiops/sessions/models.py:8` 与 `src/aiops/migrations/0012_sessions.sql:4` 当前仅登记人工和告警会话。会话创建及事件追加在 `src/aiops/sessions/store.py:55`、`src/aiops/sessions/store.py:202`。

## 变更范围

- 新建 `src/aiops/inspection/` 和 `skills/proactive-inspection/SKILL.md`，实现清单校验、注册步骤、范围受限的只读采集及结果归档。SOP 里的步骤标识只接预注册适配器。样本文本和 Skill 正文都按数据处理。
- `src/aiops/serve.py`、`src/aiops/config.py`、`src/aiops/investigation/factory.py` 接入调度与只读客户端。复用 Kubernetes 和 Prometheus 访问及其超时约束。指标以源样本时间验证新鲜度。查询评估时间和响应时间不得冒充样本时间。
- `src/aiops/migrations/` 追加会话来源、轮次、观察和初始摘要的存储。用数据库原子领取时点和活动轮次，记录持有者、代次、截止时间，写回与创建工作同事务检查归属。每时点去重，跳过后按固定节拍继续。
- `src/aiops/sessions/models.py`、`src/aiops/sessions/store.py` 增加 `inspection` 来源和标题、内部创建、事件、异常轮入队和历史查询。距截止不足时直接结轮。巡检异常只能从内部入口入队，常规队列不能重领已超时或中断的主线。`src/aiops/serve.py:66` 的会话驱动接入轮次截止。轮次结束后只允许已持久化动作的可信回执继续写入历史。

## 风险与回滚

- 生产用数据库 `clock_timestamp()` 裁决到期、租约和发送权，授权在取得相关锁后取时。测试通过调用边界注入同一时钟，覆盖应用观察窗与涉及的 SQL 到期、领取、续租、重新排期和发送判断。真实事务和约束照常执行，测试用显式驱动队列或唤醒机制推进。生产配置不暴露时钟覆盖入口。
- 租约未过期时原进程继续。到期结算后停止旧主线，清理待领取工作后再释放活动轮次。检查范围和结果出站前经过既有脱敏与裁剪。
- 截止或失去租约后，旧任务即使迟到返回也无法覆盖摘要、补入调查或新建动作。已进入处置账本的任务按处置子计划收尾。
- 关闭巡检后保留已有记录和兼容读取。遗留会话和迁移数据进入升级回归。

## 验证

- 快测新增 `tests/test_inspection_run.py` 后执行 `uv run --frozen pytest -q tests/test_inspection_run.py tests/test_skills.py tests/test_monitoring_snapshot.py`。覆盖全健康、局部失败、过期样本、超时、跳过和两个进程竞争。
- 设置独占 `VIGIL_DATABASE_URL` 后追加 `tests/test_sessions.py` 验证。用现有隔离 schema 夹具测试旧库升级、同轮唯一、进程失联和迟到结果。
- 验证源码与已安装 wheel 都从预期目录读取巡检 Skill，内容和哈希一致。覆盖配置指定目录里的有效与无效 SOP。
- 合入前执行总纲 Linux 门禁。

## 停止条件

- 需要新增节点或入口写权限，或需要从自然语言解析任意工具参数。
- 失效持有者仍能写回，或调度依赖数据库之外的全局进程锁。

## 进度

- [ ] 接入配置、巡检 Skill 和固定只读步骤。
- [ ] 落库轮次、观察和摘要，挂接定时领取与中断恢复。
- [ ] 跑通假时钟和 PostgreSQL 调度验证。

## 范围外的问题

- 巡检时间表管理页面和历史归档策略。
