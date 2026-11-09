# V07 证据回查修复与本地主干同步

状态：2026-11-09 操作者已确认第 5 节决定，待计划审批后执行。

## 1. Context

- 本地 `main` 落后 `origin/main` 24 个提交，领先 2 个（`be0d4c7` env 端口、`4391af2` 操作手册），分叉点 `9ab2868`。
- 远端已合入 PR #1（V09 规则基线对照）和 PR #2（文档对齐“完整 MVP 已交付”）。此前“V09 有 bug”的判断基于本地旧代码，已作废。
- 2026-11-09 用 `origin/main` 的代码和清单对当前库重跑 `aiops eval`：V09 通过，**V07 未通过**。远端文档写的“V01–V10 满足”在今天不能复现。
- V07 失败原因：清单 `trace_runs` 指向 `inv-4ce0757b47b0`，它 6 条集群证据的原始文件 `runtime/evidence/inv-4ce0757b47b0/*.json` 已不存在。全库 447 条证据、79 个调查，`runtime/` 只剩今天 3 个调查的文件。删除原因无法从现有记录判断。
- 校验逻辑本身没错：`eval.py` 要求 `raw_ref` 落在 `evidence_root`（默认 `runtime`）内且文件非空。问题在于验收依赖一个被 gitignore、随时可清的目录。

## 2. Approach

1. 新建分支 `fix/v07-evidence-usage`，把本地两个提交（`be0d4c7`、`4391af2`）放到这个分支并 rebase 到 `origin/main`。V07 修复也提交在同一分支，一个 PR 交付。五份冲突文档的状态行以远端为准，只保留 `docs/usage.md`、`.env.example`、`monitoring.yaml` 和入口处指向操作手册的链接。
2. V07 回查用例改指向今天真实告警触发的调查 `inv-d75993aade89`（14 条证据，原始文件齐全）。
3. 把这次调查的原始证据归档进仓库 `demos/ms08/runtime/evidence/inv-d75993aade89/`，清单的 `evidence_root` 改为 `demos/ms08/runtime`。验收从此不依赖本地 `runtime/`。
4. 在 MS08 报告里如实记录：V07 曾在 2026-11-09 复核时失败，原因是历史原始文件丢失；历史 76 个调查的原始观测不可回查，只能重跑。
5. 补一个单测：原始文件缺失时 V07 失败（防回归）；补一条使用说明：`runtime/` 不是证据存档。

## 3. Files to modify

| 文件 | 改动 |
|---|---|
| `eval/scenarios.json` | `trace_runs` 改指向 `inv-d75993aade89`；`evidence_root` 改为 `demos/ms08/runtime` |
| `demos/ms08/runtime/evidence/inv-d75993aade89/*.json` | 新增，12 个文件约 96K，从 `runtime/` 复制 |
| `demos/ms08/README.md` | V07 行补复核记录与历史缺口（只改此处，不改 README 状态行） |
| `demos/ms08/outputs/` | 新增修复前后两次 `aiops eval` 输出 |
| `demos/ms08/screenshots/v07/` | 修复前失败、修复后通过、证据文件存在三张截图 |
| `docs/usage.md` | 第 7 节补“`runtime/` 会被清，验收证据要归档” |
| `tests/test_eval_v07.py` | 确认已有缺失文件负例；没有就补一条 |

## 4. Reuse

- `eval.py` 的 `trace_runs` 校验与 `evidence_root` 字段已存在，不改校验逻辑。
- `_evidence_problems` 的 markers 机制：回查用例的说明文件沿用 `demos/ms06/README.md` 的写法，或在 MS08 报告里写标记。
- `scripts/render_evidence.py`：确认能否直接生成终端截图，不能再手工截。

## 5. 已定决定（2026-11-09 操作者确认）

1. 本地两个提交和 V07 修复放在同一个分支 `fix/v07-evidence-usage`，rebase 到 `origin/main`，开一个 PR。本地 `main` 指针移到 `origin/main`（已授权改写本地 `main`）。
2. 操作者确认证据不含敏感内容。执行时仍逐个快速核对（token、Authorization、password、环境变量）；若意外发现，停下报告，不自行脱敏。
3. “V01–V10 满足”的表述只在 MS08 报告加复核记录，README 与其他状态行不改。
4. 走 PR，截图用指向提交 SHA 的 raw 地址；合并由操作者决定，不开 auto-merge。

## 6. Steps

- [x] 删除临时目录 `/tmp/vigil-upstream`（里面有 `.env` 副本）
- [x] `git switch -c fix/v07-evidence-usage`（从当前本地 `main`，带上两个提交），`git branch -f main origin/main`，再 `git rebase origin/main`；冲突的状态行以远端为准，只保留操作手册链接，`make check-all`
- [x] 修复前跑一次 `aiops eval`，保存输出和截图
- [x] 检查 `inv-d75993aade89` 的 12 个原始文件有无敏感内容
- [x] 复制原始证据到 `demos/ms08/runtime/`，改清单
- [x] 补或确认 V07 缺失文件负例
- [x] 修复后跑 `aiops eval`，V01–V10 全部通过，保存输出和截图
- [x] 更新 MS08 报告与 `docs/usage.md`
- [ ] V07 修复按文件分组提交到同一分支，`git push -u origin fix/v07-evidence-usage`、`gh pr create --base main`。PR 描述覆盖操作手册、webhook 端口和 V07 三部分，评论用提交 SHA 的 raw 地址附截图；停在待合并

## 7. Verification

- `make check-all` 全部通过。
- `aiops eval` 输出 V07 通过，其余九条不退化。
- 负例：临时把归档目录中一个文件改名，V07 必须失败；恢复后通过。
- 删除本地 `runtime/evidence/inv-d75993aade89` 的副本后再跑 eval，V07 仍通过，证明不再依赖 `runtime/`。这一步只在副本备份后做。
