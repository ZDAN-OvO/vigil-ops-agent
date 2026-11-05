# V06 异常结果停止与待办 plan

V06 要求执行结果能区分无改善、恶化与未知，停止同目标后续自动动作，并留下人工待办。程序分两个 PR。V06-1 增加动作前基线与结果四分类。V06-2 增加待办持久化、CLI 与 V06 评估断言。重启安全用例在 `vigil-experiment` 的可丢弃工作负载上运行；回退用例在 `vigil-shop` 的 F01 坏发布上运行，由操作者恢复。

## How to read this

One box is one unit of work. Every box names the evidence that checks it. A nested box is a sub-step of the box above it. Check a box only when its evidence exists, a file, a log line, a screenshot, a test run, or a SHA. The body is a how-to. The appendices explain and record.

The program runs `pstack/skills/poteto-mode/playbooks/autopilot-full.md` from the installed pstack plugin. The agent merges each MVP PR after its own review, and the operator spot-checks merged PRs. No PR in this program is review-gated.

Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

## Program checklist

### Arm the program

- [ ] State the protocol and this plan to the operator, then stop. Start execution only on the operator's explicit go.
- [ ] Prototype the poison switch on a throwaway branch before writing PR 1 code. Build the A01 image, import it into `vigil-lab`, confirm a new Pod reads the mounted ConfigMap and corrupts its own state. Record the branch, SHA and screenshot in Appendix A.
- [ ] Read these from trunk at program start. Re-read them at every tick.
  - [ ] `git show origin/main:AGENTS.md`
  - [ ] `git show origin/main:docs/contracts.md`
  - [ ] `git show origin/main:docs/mvp.md`
  - [ ] Read the execution playbook at `/home/zdan/.claude/plugins/cache/cursor-plugins/pstack/0.15.36/skills/poteto-mode/playbooks/autopilot-full.md`.
  - [ ] Read the control skill at `/home/zdan/.claude/plugins/marketplaces/cursor-plugins/cursor-team-kit/skills/control-cli/SKILL.md`.
- [ ] On the operator's go, arm the audit tick as `/loop 1h` with the tick prompt below. Never leave the cadence to memory.
- [ ] Use this tick prompt, verbatim. "Re-read the execution playbook from trunk. Audit the operation against it and fix drift in this tick. Probe every active lane and judge progress by side effects only. Stand down a stuck lane and dispatch its replacement now. Then post a short status message to the operator in chat only when the audit found a tracked change that no earlier status message reported, such as a PR opened, a code-ready head, a round launched or closed, a verdict, a merge, a stuck agent and the action taken, a blocker added or cleared, or a decision only the operator can make. Name every such change and nothing else. Do not repeat a table, the merged list, or an unchanged blocker. If the audit found none, end the turn with no reply text. Either way, log this tick's row in your decision trail. The row names the items reported, or none."
- [ ] On the operator's hold or stand-down, send every owner a zero-writes order at once.

### Spawn owners

- [ ] Spawn one owner per PR with the full lifecycle the execution playbook names.
- [ ] Follow this dependency graph. Start dependent work only after its parent merges, or base it on the parent branch when the execution playbook stacks.
  - [ ] V06-1 branches from `main` and lands first.
  - [ ] V06-2 branches from `main` after V06-1 merges.
- [ ] Hold the file boundaries. V06-1 touches only `src/aiops/actions/`, `src/aiops/contracts.py`, `faults/a01-pod-rebuild/`, `tests/test_actions.py` and `docs/contracts.md`. V06-2 touches only `src/aiops/migrations/`, `src/aiops/db.py`, `src/aiops/contracts.py`, `src/aiops/actions/executor.py`, `src/aiops/cli.py`, `src/aiops/eval.py`, `eval/scenarios.json`, the related tests, `docs/contracts.md` and `demos/ms08/`. The two PRs share `contracts.py` and one executor file, so they never run in parallel.
- [ ] Hold the review gate. No PR is review-gated. The agent merges after a clean independent review, and the operator spot-checks merged PRs, per the standing MVP policy.

### PR mechanics, for every PR

- [ ] Resolve the forge once. Default to `gh`. Record any fallback.
- [ ] Open the PR ready, never draft. Base it on `main`. Use `gh pr create --base main`.
- [ ] Run `make check-all` once before the PR-facing push. Push with hooks on.
- [ ] Run the installed review skills where available. Use `pstack:comment-sicko` for comment hygiene. Record any missing skill and the substitute check.
- [ ] Triage every Bugbot and security-reviewer comment. Fix real defects, dismiss noise with a concrete reason.
- [ ] Rebase onto current trunk before the code-ready report and babysit. Keep that merge base in fix rounds.

### Verdict and merge, for every PR

- [ ] Run a full swarm once, at the code-ready head. One gates lane, the ten live lanes from the PR's live block, the perf lane, and two audit lanes that read the diff and receipts and distrust the PR body.
- [ ] Judge every finding by the blocking rule. The verdict is clean when none blocks. A failure on trunk behavior in the regression lane is recorded against trunk and does not block the PR.
- [ ] The agent merges its own PR after a clean verdict, rebase and Bugbot triage, per the standing MVP policy. The operator spot-checks the merged PR. No merge happens on a dirty verdict.

### Boot recipe, for every live lane

Each lane runs on the local `vigil-lab` k3d cluster, sequentially, with namespace cleanup between lanes. The backend is PostgreSQL at `127.0.0.1:55432` and the cluster's API server.

- [ ] `git fetch origin <head-branch> && git checkout <head SHA>`.
- [ ] Confirm `k3d cluster list` shows `vigil-lab`, and `kubectl --kubeconfig ~/.kube/vigil-lab.yaml get ns` succeeds. For rollout lanes, start `kubectl port-forward -n vigil-monitoring svc/prometheus 9090:9090` and wait for `probe_success` to answer.
- [ ] Deliver input only through `aiops` commands. Read-only diagnostics are `kubectl`, `psql` and the AIOps CLI `proposal`, `approvals` and `followups` commands.
- [ ] Save every screenshot to `/tmp/swarm-v06/<pr-id>/worker-<n>/<slug>.png` and return the paths with the report. Copy the final set into `demos/ms08/screenshots/v06/`.

## 分类执行结果并采集动作前基线 (V06-1)

**Depends on.** None.

**Files.**

- [ ] Edit `src/aiops/contracts.py`.
- [ ] Create `src/aiops/actions/outcome.py`.
- [ ] Edit `src/aiops/actions/executor.py`.
- [ ] Edit `src/aiops/actions/restart_pod.py`.
- [ ] Edit `faults/a01-pod-rebuild/app.py`.
- [ ] Edit `faults/a01-pod-rebuild/manifests.yaml`.
- [ ] Create `faults/a01-pod-rebuild/v06.sh`.
- [ ] Edit `tests/test_actions.py`.
- [ ] Create `tests/test_outcome.py`.
- [ ] Edit `tests/test_a01_script.py`.
- [ ] Edit `faults/a01-pod-rebuild/README.md`.
- [ ] Edit `docs/contracts.md`.

**Build.**

- [ ] Add `AttemptOutcome` with `improved`, `no_improvement`, `worsened`, `unknown` in `src/aiops/contracts.py`.
- [ ] Add `classify_outcome(baseline, post)` in `src/aiops/actions/outcome.py`. It returns the outcome, reason and the frozen threshold. The margin is 0.5.
- [ ] In `_execute_restart`, sample the old Pod with `pod_get` before `dry_run` and before delete. Use `_min_samples` samples at `_sample_seconds`. Abort with a failed proposal and no attempt row when the baseline cannot be sampled.
- [ ] In `_execute_rollout`, query `probe_http_status_code` over the `_verify_seconds` window before `apply`. Exclude 0, 401, 403, 502, 503 and 504 as no business response; any other non-2xx is a business failure. Abort before the write when the baseline has fewer than `_min_samples` valid points or the query fails.
- [ ] Write `baseline`, `post`, `outcome` and `outcome_reason` into the verification payload. Map improved to verified, and no_improvement and worsened to failed, and unknown to unknown.
- [ ] Replace the restart dry-run with a read-only UID re-read and `SelfSubjectAccessReview`. This cluster applies a delete request even with `dryRun=All`, which was deleting the target Pod before the real action.
- [ ] Add a startup poison switch to the A01 app. The app reads `/config/poison` at startup and writes corrupt state when the content is `yes`.
- [ ] Add the `rebuild-demo-poison` ConfigMap and its volume mount to the A01 manifests.
- [ ] Add `v06.sh` with `deploy`, `poison-on`, `corrupt`, `restart`, `second-proposal` and `cleanup` subcommands. The script refuses any namespace other than `vigil-experiment`.

**You see.**

- [ ] A healthy restart writes `verification.outcome=improved` and keeps proposal `succeeded`.
- [ ] A failing replacement writes `verification.outcome=no_improvement` or `worsened` and proposal `failed`, with baseline and post sample counts in the record.
- [ ] Insufficient post samples write `verification.outcome=unknown` and proposal `unknown`.
- [ ] A missing baseline stops before delete and writes no attempt row.

**Verify, unit.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Add classification tests for the four outcomes and the 0.5 boundary in `tests/test_actions.py`.
- [ ] Add a test that a missing baseline aborts before `adapter.execute` and before `save_attempt`.
- [ ] Add mapping tests for attempt and proposal status per outcome.
- [ ] Update the existing restart and rollout tests for the new baseline call.
- [ ] Run `make check-all`.

**Verify, live.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked. Ten lanes on `sonnet max` at the PR head, per the boot recipe.

- [ ] Lane 1. Regression lane against trunk. Run a healthy restart on trunk and on head. Trunk has no `outcome` field, so record that fact and gate head verification `outcome=improved` plus proposal `succeeded`. Save `01-regression.png`. Pass when both attempts are verified and the head record carries `outcome=improved`.
- [ ] Lane 2. Auto restart on a healthy discardable Pod. Save `02-improved.png`. Pass when the proposal is `succeeded` and `outcome=improved`.
- [ ] Lane 3. Corrupt the current Pod state, enable poison, and run the auto restart. Save `03-no-improvement.png`. Pass when the replacement stays 500, proposal is `failed`, and `outcome=no_improvement`.
- [ ] Lane 4. Keep the current Pod healthy, enable poison, and run the auto restart. Save `04-worsened.png`. Pass when the replacement returns 500, proposal is `failed`, and `outcome=worsened`.
- [ ] Lane 5. Run a restart with `VIGIL_ACTION_VERIFY_SECONDS=5` and `VIGIL_ACTION_SAMPLE_SECONDS=15`. Save `05-unknown.png`. Pass when post samples are insufficient, proposal is `unknown`, and `outcome=unknown`.
- [ ] Lane 6. Run rollout undo on the F01 bad release in `vigil-shop` with Prometheus reachable. Wait until `probe_success` has been 0 for the full baseline window, then act. Save `06-rollout-improved.png`. Pass when the whole pre-window probe is failing, the post-window tail is healthy, and `outcome=improved`.
- [ ] Lane 7. Make Prometheus unreachable (unreachable URL or stopped port-forward), then run rollout undo on the F01 bad release. Save `07-baseline-stop.png`. Pass when the proposal fails before apply, no attempt row exists, and the Deployment revision is unchanged.
- [ ] Lane 8. Record the Pod UID and Service selector, break the selector, and run restart on that Pod. Restore the selector, then record the UID again. Save `08-service-stop.png`. Pass when the proposal fails before delete, no attempt row exists, and the Pod UID is unchanged.
- [ ] Lane 9. After Lane 3 or Lane 4, submit a second restart for the same workload. Save `09-stop-gate.png`. Pass when the second disposition is `approval` and the workload still has one attempt.
- [ ] Lane 10. Clean up the experiment namespace. Save `10-cleanup.png`. Pass when the namespace is absent and `vigil-shop` still reports 12 Deployments.

**Verify, perf.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Metric. Wall-clock time from execute start to attempt finish for a healthy restart, trunk against head, plus the head baseline sample count.
- [ ] Probe. Run the same healthy restart command on trunk and on head, interleaved, with the same namespace and workload.
- [ ] Baseline. Record the trunk duration first.
- [ ] Rule. Head minus trunk must stay within 60 seconds, and the head attempt must still be `verified`. A larger increase fails the lane.

**Review gate.** None. V06-1 is not review-gated.

**Merge.**

- [ ] Clean verdict at the exact head SHA.
- [ ] Bugbot and security-reviewer triage done.
- [ ] Rebased onto current trunk after the verdict, patch-id unchanged.
- [ ] Agent merges the PR, and the operator spot-checks it.

## 持久化待办并接入 V06 断言 (V06-2)

**Depends on.** V06-1.

**Files.**

- [ ] Create `src/aiops/migrations/0007_action_followups.sql`.
- [ ] Edit `src/aiops/db.py`.
- [ ] Edit `src/aiops/contracts.py`.
- [ ] Edit `src/aiops/actions/executor.py`.
- [ ] Edit `src/aiops/cli.py`.
- [ ] Edit `src/aiops/eval.py`.
- [ ] Edit `eval/scenarios.json`.
- [ ] Edit `tests/test_actions.py`.
- [ ] Edit `tests/test_cli.py`.
- [ ] Edit `tests/test_eval.py`.
- [ ] Edit `docs/contracts.md`.
- [ ] Edit `demos/ms08/README.md`.
- [ ] Create `demos/ms08/outputs/v06/`.
- [ ] Create `demos/ms08/screenshots/v06/`.

**Build.**

- [ ] Add the `action_followups` table with proposal and attempt references, outcome, reason, evidence JSONB, status and resolution fields.
- [ ] Add `ActionFollowup` to `src/aiops/contracts.py`.
- [ ] Add `finish_attempt` to `src/aiops/db.py`. It updates the attempt, updates the proposal status and inserts the followup in one transaction.
- [ ] Add `save_followup`, `list_followups` and `resolve_followup` to `src/aiops/db.py`.
- [ ] In `_finish`, create a followup for no_improvement, worsened and unknown. Do not create one for improved or for a pre-write failure.
- [ ] Add `aiops followups` and `aiops followup <id> --resolve --actor --note` to `src/aiops/cli.py`.
- [ ] Add outcome and followup id to the `act` and `proposal` text and JSON output.
- [ ] Add `safety_runs` to the manifest model in `src/aiops/eval.py`. Each entry names the run, the expected outcome and the proposal or attempt.
- [ ] Implement the V06 criterion. It requires one registered run for each of no_improvement, worsened, unknown and insufficient evidence, a matching attempt record, and an open followup for the three action outcomes.
- [ ] Register the V06 runs and evidence paths in `eval/scenarios.json`.

**You see.**

- [ ] A failed or unknown outcome produces one open followup with the reason and evidence snapshot.
- [ ] `aiops followups` lists open items with proposal and attempt ids.
- [ ] Resolving an item removes it from the open list and writes who resolved it.
- [ ] `aiops eval` reports V06 as passed only when all four categories are registered and match the database.

**Verify, unit.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Add tests for followup creation per outcome and for no followup on improved.
- [ ] Add a test that the attempt update and followup insert roll back together on a forced insert failure.
- [ ] Add CLI tests for listing, resolving and rejecting an unknown followup id.
- [ ] Add eval tests for the four-category rule, a missing category and a mismatched attempt id.
- [ ] Run `make check-all`.

**Verify, live.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked. Ten lanes on `sonnet max` at the PR head, per the boot recipe.

- [ ] Lane 1. Regression lane against trunk. On trunk, `aiops followups` does not exist. Record that fact and gate the head listing plus a normal improved run creating no followup. Save `11-regression.png`. Pass when the head lists open items and the improved run leaves the count unchanged.
- [ ] Lane 2. Reproduce the no-improvement case and open its followup. Save `12-no-improvement-followup.png`. Pass when one open followup carries `no_improvement` and the attempt id.
- [ ] Lane 3. Reproduce the worsened case and open its followup. Save `13-worsened-followup.png`. Pass when one open followup carries `worsened`.
- [ ] Lane 4. Reproduce the unknown case and open its followup. Save `14-unknown-followup.png`. Pass when one open followup carries `unknown`.
- [ ] Lane 5. List open followups. Save `15-followups-list.png`. Pass when the three items appear with their proposal and attempt ids.
- [ ] Lane 6. Resolve one followup with an actor and note. Save `16-followup-resolve.png`. Pass when the open list drops it and the audit records the resolver.
- [ ] Lane 7. Submit a second proposal after a failed outcome. Save `17-stop-gate.png`. Pass when the disposition is `approval`, one attempt exists for the workload, and the followup stays open.
- [ ] Lane 8. Run `aiops eval --manifest-path eval/scenarios.json`. Save `18-eval-v06.png`. Pass when V06 passes and V01 to V04 and V10 keep their previous results.
- [ ] Lane 9. Run the same command on a copy of the manifest with the worsened run removed. Save `19-eval-v06-negative.png`. Pass when V06 fails and names the missing category.
- [ ] Lane 10. Clean up and verify the evidence pack. Save `20-cleanup.png`. Pass when the namespace is absent, `vigil-shop` has 12 Deployments, and the output and screenshot directories exist.

**Verify, perf.** Tests alone are not sufficient verification. A PR is verified only when its unit, live, and perf boxes are all checked.

- [ ] Metric. Wall-clock time from delete to attempt finish for a failed outcome, trunk against head, plus the followup write count.
- [ ] Probe. Run the same no-improvement scenario on trunk and on head, interleaved.
- [ ] Baseline. Record the trunk duration first.
- [ ] Rule. Head minus trunk must stay within 2 seconds, and the head must produce exactly one followup row.

**Review gate.** None. V06-2 is not review-gated.

**Merge.**

- [ ] Clean verdict at the exact head SHA.
- [ ] Bugbot and security-reviewer triage done.
- [ ] Rebased onto current trunk after the verdict, patch-id unchanged.
- [ ] Agent merges the PR, and the operator spot-checks it.

## Close the program

- [ ] Every box above is checked with its evidence.
- [ ] Reply to the operator with the merged PR ids, the V06 result, the remaining V05, V07 and V08 gaps, and the evidence paths.

## Appendix A. Prototype evidence

The poison switch was the only empirical question. The throwaway branch `proto/v06-poison` at `861e2a1` built `vigil/pod-rebuild-demo:v06-proto`, imported it into `vigil-lab`, and proved the mechanism. The old Pod kept returning 200. After the ConfigMap was switched to `yes`, the replacement Pod started with `state_file=corrupt` and three `/work` requests all returned 500. Evidence is in `demos/ms08/outputs/v06/00-prototype.txt` and `demos/ms08/screenshots/v06/00-prototype.png`.

The switch is now part of V06-1, and the image tag is `20261105-v06`. No other question needed a prototype.

## Appendix B. Alternatives rejected

- Detect only failed versus verified without a baseline. Rejected because failed cannot tell no improvement from deterioration.
- Add new `AttemptStatus` values. Rejected because the status is consumed by the stop gate, CLI and tests. The outcome belongs in verification.
- Reuse proposal status as the todo. Rejected because V06 requires a persisted todo with a reason and a lifecycle.
- Recompute the baseline from investigation evidence. Rejected because the action path must not depend on an investigation existing, and the samples must match the verification path.

## Appendix C. Risks

- The poison switch is new experiment infrastructure. If the ConfigMap volume does not reach a fresh Pod promptly, Lane 3 and Lane 4 stall. The prototype in Appendix A removes this risk before PR 1.
- Baseline sampling adds about 45 seconds to every restart. Perf fails if the increase exceeds 60 seconds.
- The unknown live case uses a shortened verification window. It proves the no-data path, not a production recommendation.
- Rollout uses `probe_http_status_code`. A 0 means no response and is not a business sample; a non-2xx response is a business failure. Probe transport failures therefore stop the action or leave the tail unknown instead of counting as business failures.
- Live lanes run sequentially on one local cluster. A leaked namespace or port-forward breaks the next lane. The cleanup lane and the namespace ownership guard cover it.
- `control-cli` is present in the plugin marketplace but not installed as a cached skill. If it cannot be enabled, the lanes drive the CLI directly and record the substitution.

## Appendix D. Links and reading list

- [MVP 门槛](../mvp.md#6-完整-mvp-验收门槛) and [契约](../contracts.md) before editing.
- [决策 D15](../decisions.md) for the stop-and-todo direction.
- [评估约定](../eval.md) for the V06 assertion boundary.
- V06-1 and V06-2 both get a how review and an interrogate review before their verdicts.
- Decision trail per the show-me-your-work skill, kept local unless the stakes need an auditable record.
