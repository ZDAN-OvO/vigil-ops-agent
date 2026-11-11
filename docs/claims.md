# 主张—证据矩阵

README 与 `demos/` 里的每条结论，在这里对应到一手证据和核对方式。改结论时同步改这张表；证据过期时，最后两列说明谁会发现。

| 主张 | 一手证据 | 自动核对 | 过期谁发现 |
|---|---|---|---|
| 真实告警能触发调查 | `demos/ms01/outputs/03-alert-entry.txt`；调查 `inv-d75993aade89`（库里可查） | 否，需要数据库 | `aiops eval` V07、V10 |
| 调查主循环是 LangChain `create_agent`，不是手写状态机 | `src/aiops/agent/engine.py`；[ADR-0005](adr/0005-langchain-single-agent-loop.md)；`tests/test_agent_flow.py` | 是（`make check-all`） | CI |
| 调查取证只用只读工具，动作请求单独成批并受策略约束；工具、模型、墙钟三重预算 | `src/aiops/agent/tools.py`、`middleware.py`、`budget.py`；`tests/test_agent_budget.py` | 是 | CI |
| 墙钟与模型调用是硬截止，单次调用不超过剩余时间 | `src/aiops/agent/budget.py` 的 `model_call_timeout`／`read_tool_timeout`；`tests/test_agent_budget.py` | 是 | CI |
| 工具调用按 `(investigation_id, tool_call_id)` 幂等，重放不二次写 | `src/aiops/migrations/0010_agent_task_and_ledger.sql`、`src/aiops/agent/tools.py`、`tests/test_agent_recovery.py` | 是 | CI |
| 任务运行状态与诊断结论分离，`data.task` 可查 | [ADR-0006](adr/0006-task-outcome-separation.md)、`src/aiops/contracts.py` 的 `InvestigationTask`、`tests/test_db.py` | 是 | CI |
| 预算数值来自实测，不是拍的 | `docs/decisions.md` D26；`demos/ms08/outputs/v2/f03-measure*.txt` 用量 17/14/21；2026-11-11 主循环把模型调用由 10 修订为 20（真实审批反馈需要），记录在 `PLAN.md` 与 `demos/langchain-loop/README.md` | 否 | 审查：改数值要重测 |
| 写集群只有两种动作，且先过规则 | `src/aiops/actions/policy.py`；`tests/test_actions_policy.py` | 是 | CI |
| 审批持久化、可跨进程恢复，过期或漂移不执行 | `demos/ms02/outputs/`；`tests/test_actions_rollback.py` | 部分（单测），实机靠清单 | `aiops eval` V05 |
| 恢复由真实请求验证，不看 Pod Ready | `src/aiops/actions/outcome.py`；`demos/ms01/outputs/07-recovery.txt` | 部分（单测） | `aiops eval` V03 |
| V01–V10 通过 | `eval/scenarios.json`；`demos/ms08/outputs/21-v07-eval-after.txt`；v2 运行在 `demos/ms08/outputs/v2/` | 否，需要数据库 | 手动 `uv run aiops eval` |
| Agent v2 在三个核心场景各 3 次都正确 | `demos/ms08/README.md` 的「Agent v2」段；`outputs/v2/f01-run{3,4,5}.txt`、`f02-run{1,2,3}.txt`、`f03-confirm{1,2,3}.txt` | 否 | 审查：清单里的调查 ID 可逐条查库 |
| v2 运行只建议动作，正式提案靠去重后残留（f01 一条、f02 一条、f03 无） | `eval/scenarios.json` 里 v2 各运行的 `proposal_id` 为空；`demos/ms08` 的 v2 表脚注 | 否 | 审查：库里 `action_proposals.investigation_id` 查得到 |
| 旧 a01 评分不代表调查闭环，但单 Agent 已能自主提出 `restart_pod` 并恢复业务 | `eval/scenarios.json` 的 a01 旧记录 `investigation_id` 为空；补充运行 `inv-988f6a59c808`（提案 `act-de5b0f7c1791`，UID 变化、模板不变、outcome improved）见 `demos/langchain-loop/README.md` | 否 | 审查：不会追溯改写旧评分 |
| 已知弱点如实登记（f03 旧预算 1/4、a01 旧批次不评调查、历史证据丢失） | `demos/ms08/README.md` 的缺口段；`eval/scenarios.json` 的 `f03`、`a01` 运行记录 | 否 | 审查；AI 扫仓库时最先对的就是这里 |
| 旧 `.env` 的三个值已停用 | 用户在 2026-11-09 确认；README 末尾说明 | 否 | 审查 |

## 怎么读「自动核对」列

`是` 表示 `make check-all` 覆盖到，CI 每次推送都会跑（`.github/workflows/checks.yml`）。

`部分` 表示只有单元测试能覆盖分支逻辑，真实数据库或集群上的行为要看清单与证据。

`否` 表示没有自动检查：要么需要活的 PostgreSQL（`aiops eval`），要么需要人读。这类主张最容易过期，改结论时必须回头核对。

## 谁维护

改 README 结论的人同步改这张表；新增主张就新增一行，删主张就删行，不能留「曾经通过」的空行。
