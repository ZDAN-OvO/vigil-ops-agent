# 主张—证据矩阵

README 与 `demos/` 里的每条结论，在这里对应到一手证据和核对方式。改结论时同步改这张表；证据过期时，最后两列说明谁会发现。

| 主张 | 一手证据 | 自动核对 | 过期谁发现 |
|---|---|---|---|
| 真实告警能触发调查 | `demos/ms01/outputs/03-alert-entry.txt`；调查 `inv-d75993aade89`（库里可查） | 否，需要数据库 | `aiops eval` V07、V10 |
| 调查只用只读工具，有工具／模型／墙钟三重预算 | `src/aiops/investigation/runtime.py` 的 `_model_budget_block`、`_tool_budget_block`；`tests/test_investigation_flow.py` | 是（`make check-all`） | CI |
| 墙钟是硬截止，单次模型调用不超过剩余时间 | `src/aiops/investigation/runtime.py` 的 `_model_timeout_now`；测试 `test_model_call_never_exceeds_remaining_wall_clock` | 是 | CI |
| 预算数值来自实测，不是拍的 | `docs/decisions.md` D26；`demos/ms08/outputs/v2/f03-measure*.txt` 用量 17/14/21 | 否 | 审查：改数值要重测 |
| 写集群只有两种动作，且先过规则 | `src/aiops/actions/policy.py`；`tests/test_actions_policy.py` | 是 | CI |
| 审批持久化、可跨进程恢复，过期或漂移不执行 | `demos/ms02/outputs/`；`tests/test_actions_rollback.py` | 部分（单测），实机靠清单 | `aiops eval` V05 |
| 恢复由真实请求验证，不看 Pod Ready | `src/aiops/actions/outcome.py`；`demos/ms01/outputs/07-recovery.txt` | 部分（单测） | `aiops eval` V03 |
| V01–V10 通过 | `eval/scenarios.json`；`demos/ms08/outputs/21-v07-eval-after.txt`；v2 运行在 `demos/ms08/outputs/v2/` | 否，需要数据库 | 手动 `uv run aiops eval` |
| Agent v2 在三个核心场景各 3 次都正确 | `demos/ms08/README.md` 的「Agent v2」段；`outputs/v2/f01-run{3,4,5}.txt`、`f02-run{1,2,3}.txt`、`f03-confirm{1,2,3}.txt` | 否 | 审查：清单里的调查 ID 可逐条查库 |
| 已知弱点如实登记（f03 旧预算 1/4、a01 不评调查、历史证据丢失） | `demos/ms08/README.md` 的缺口段；`eval/scenarios.json` 的 `f03`、`a01` 运行记录 | 否 | 审查；AI 扫仓库时最先对的就是这里 |
| 旧 `.env` 的三个值已停用 | 用户在 2026-11-09 确认；README 末尾说明 | 否 | 审查 |

## 怎么读「自动核对」列

`是` 表示 `make check-all` 覆盖到，CI 每次推送都会跑（`.github/workflows/checks.yml`）。

`部分` 表示只有单元测试能覆盖分支逻辑，真实数据库或集群上的行为要看清单与证据。

`否` 表示没有自动检查：要么需要活的 PostgreSQL（`aiops eval`），要么需要人读。这类主张最容易过期，改结论时必须回头核对。

## 谁维护

改 README 结论的人同步改这张表；新增主张就新增一行，删主张就删行，不能留「曾经通过」的空行。
