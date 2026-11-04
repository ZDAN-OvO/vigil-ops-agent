# MS01 证据报告：坏发布从真实告警到受控回退

状态：2026-11-04 在 `vigil-lab` 完成第一次端到端运行，八个步骤全部走到，链路真实。第 5 步的提案由操作者发起，不是模型提案；其余缺口见第 6 节。本报告只按实际状态陈述，不把未完成的接线算作已实现。

## 1. 结论

- 真实告警触发、webhook 建事件、模型只读取证、人工审批、受控回退、业务恢复验证都在同一次运行中完成。
- 回退由人工审批后执行，业务恢复由 `probe_success` 末段连续样本判定，不是"Pod Ready 即恢复"。
- 调查结论指向前端下游的近期滚动，但模型自己标明因果链尚未证实，工具预算在 8 次时耗尽。
- 主运行的提案由操作者发起；同日的补充运行已经接通模型提案，并跑通"告警 → 模型提案 → 审批 → 执行"（第 7 节）。

## 2. 环境与版本

| 项目 | 值 |
|---|---|
| 集群 | `vigil-lab`，k3s v1.35.5+k3s1，单节点 |
| 业务 | Online Boutique v0.9.0，12 个 Deployment |
| 代码 | `fbe0a60`，分支 `fix/f01-neutral-image` |
| 实验镜像 | `vigil/productcatalogservice:canary-20261104`，中性标签与中性错误文本 |
| 模型 | 网关别名 `mythos-magpie-1505138152`，实际模型由网关决定 |
| 策略 | `mvp-policy-1`，`rollout_undo` 仅限 `vigil-shop`，首次执行需人工审批 |
| 调查预算 | 8 次工具调用、6 次模型调用、300 秒 |
| 观察窗 | 告警 `for: 1m`；恢复为滚动完成后 90 秒窗口末段 3 个样本 |

## 3. 时间线

所有时间均为 UTC。原始输出在 [`outputs/`](outputs/)，截图在 [`screenshots/`](screenshots/)。

| 时间 | 阶段 | 证据 |
|---|---|---|
| 13:40:42 | 基线：健康镜像、revision 19、`probe_success=1`、首页 200、0 告警 | [`01-baseline.txt`](outputs/01-baseline.txt)、[首页 200](screenshots/01-baseline-home-200.png) |
| 13:41:00 | 注入中性坏镜像，滚动 1 秒，revision 20 | [`02-inject.txt`](outputs/02-inject.txt) |
| 13:41:21 | 首页返回 500；此刻新 Pod 尚未就绪，页面是 dial timeout | [首页 500（滚动期）](screenshots/02-home-500.png) |
| 13:49:43 | 补充注入后页面出现应用层 500，文本为 `product catalog unavailable` | [首页 500（应用层）](screenshots/02b-home-500-app-error.png) |
| 13:42:27 | `FrontendProbeFailed` 进入 firing | [Prometheus](screenshots/03-prometheus-alerts-firing.png)、[AlertManager](screenshots/03-alertmanager-firing.png) |
| 13:42:37 | webhook 收到告警，建事件 `inc-1b7afb60bb91` 与调查 `inv-919644e08833`；重复投递返回 deduplicated | [`03-alert-entry.txt`](outputs/03-alert-entry.txt) |
| 13:44:0x | 调查完成：3 次模型调用、8 次工具调用、8 条证据、3 个假设全部被反驳或未定 | [`04-investigation.txt`](outputs/04-investigation.txt) |
| 13:44:41 | 操作者提案 `act-f363f67ddbb1`，revision 20 → 19，判定 approval，退出 3 | [`05-proposal.txt`](outputs/05-proposal.txt) |
| 13:45:19 | 人工批准，执行开始 | 见 07 的审计记录 |
| 13:47:21 | 执行状态 `verified`，窗口 7 个样本、末段 3 个全为 1 | [`06-execution.txt`](outputs/06-execution.txt) |
| 13:47:38 | 平台与业务恢复：健康 digest、revision 21、`probe_success=1`、首页 200、0 告警、坏 RS 0 | [`07-recovery.txt`](outputs/07-recovery.txt)、[首页 200](screenshots/07-home-200-recovered.png)、[告警清零](screenshots/07-prometheus-alerts-cleared.png) |

## 4. 分步说明

1. **固定基线。** 健康镜像按 digest 固定，12 个工作负载全部 1/1，`probe_success=1`，首页 200，活动告警 0。本轮没有重新采集 loadgenerator 计数，这是证据缺口。
2. **注入并确认。** 注入器用 `kubectl set image` 换成实验镜像，1 秒滚动完成，revision 从 19 到 20。旧 Pod 缩容到 0，新 Pod Ready，首页 20 秒后返回 500。
   - 滚动刚结束时页面是 `dial tcp …:3550: i/o timeout`，这是新 Pod 还没接上请求的过渡态，截图 `02-home-500.png` 记录的是这一刻。
   - 应用层稳定失败后，页面显示 `rpc error: code = Internal desc = product catalog unavailable`。主运行的时间线里用 curl 与 frontend 日志确认了这段文本；为了让截图也能证明它，13:49:37 做了一次补充注入，13:49:43 截图 `02b-home-500-app-error.png`，13:49:50 由操作者回滚并恢复 200。这次补充注入只用于取证，不参与第 3 至 7 步。
   - 两种 500 都是这次坏发布造成的真实失败，但只有应用层这一种对应场景卡的注入点。报告不把过渡态当成场景已经成立。
3. **真实告警。** Prometheus 规则按 `for: 1m` 进入 firing，AlertManager 按 `group_wait: 10s` 投递，接收端记录 202。事件上下文只有白名单标签，`scenario` 被丢弃。同一指纹再次投递返回 `deduplicated`。
4. **调查取证。** 模型先提出三个假设（工作负载不可用、Service/Endpoints 异常、网络超时），全部被证据反驳；随后指出 `productcatalogservice` 在同窗口反复滚动、镜像为 `canary-20261104`，但明确写着"因果链尚属推断，不能作为已证实结论"。工具预算在 8 次时耗尽，模型自己说明了未覆盖项。
5. **提案与授权。** 提案绑定调查 ID，目标版本为 revision 20，规则判定需要审批，命令以 3 退出，不执行。同一目标版本再次提案被 `duplicate` 拒绝；对 `kube-system` 的越界提案在读取目标阶段就因没有历史 revision 失败，属于执行前停止，未进入规则判定。
6. **执行回退。** 批准后重新读取目标，UID 与 resourceVersion 一致才继续；先写执行意图，再服务端 dry-run，再写入；滚动完成后等两个抓取周期再看 90 秒窗口。
7. **验证结果。** `probe_success` 末段 3 个样本全为 1，首页 200，活动告警清零；平台侧健康 digest 生效，坏 ReplicaSet 缩到 0。
8. **报告与复位。** 本报告关联全过程；没有独立 cleanup actor，回退即复位，坏 ReplicaSet 保留作证据。

## 5. 可查记录

- 事件：`inc-1b7afb60bb91`，`trigger=alert`，指纹 `ab769bbcfb100879`。
- 调查：`inv-919644e08833`，状态 `completed`，3 次模型调用、8 次工具调用。
- 提案：`act-f363f67ddbb1`，`origin=operator`，关联 `inv-919644e08833`。
- 审批：`ms01-demo`，`approved`，提案哈希 `7bdd7898…`。
- 审计：`action.proposed`、`action.approved`、`action.succeeded` 三条，actor 分别是操作者和 `action-executor`。
- 原始证据文件在 `runtime/evidence/`，不入库。

## 6. 失败与缺口

本次运行中出现过、或仍然存在的缺口，全部列出：

1. 主运行第 5 步由操作者手动发起，提案 `origin=operator`、`evidence_ids` 为空。"结论 → 提案"的接线在当天的补充运行中才补上（第 7 节），主运行记录保留原状。
2. 调查结论自称因果链未证实，却以 `root_cause=true` 结束，运行状态记为 `completed`。判定口径需要校准，否则"完成"会被高估。
3. 工具预算 8 次被耗尽，模型没有机会查询前端日志或下游错误率，最后一轮只能给建议。
4. 越界命名空间的实机测试没有走到规则判定：`kube-system/coredns` 只有一个 revision，在计算回退目标时先失败。规则拒绝本身只有单元测试覆盖。
5. 回退目标是"上一 revision"，不是"已验证健康的历史模板"。本次上一版本恰好健康，但系统没有事先证明它健康。
6. 没有独立 cleanup actor，回退即复位。
7. 没有 Loki 与 Tempo，模型只能看指标和 Kubernetes 对象；日志是本报告人工查看的，不是工具取回的。
8. 截图只有浏览器页面，CLI 输出以文本保存，不是终端截图。主运行第一次 500 截图抓到的还是滚动过渡态，应用层错误文本的截图来自一次补充注入。
9. 没有重新采集 loadgenerator 的错误率与延迟，第 7 步的业务恢复只用了黑盒探测和首页状态码。

## 7. 补充运行：模型提案（同日 13:55–14:11）

主运行的提案由操作者发起。当天随后补做两次运行，把"调查结论 → 提案"接上，并验证告警链路也能产出提案。

- 13:55:34 注入，13:56:58 告警。webhook 在 13:57:07 建调查 `inv-6aa44159a31e`，13:59:52 完成。模型给出了 `rollout_undo` 建议，但当时代码要求"至少一条 supporting 证据"，本次三个假设分别是 refuted、supported、refuted，提案被跳过，审计记为 `action.skipped / no_supporting_evidence`。用证据关系反推"支撑根因"并不可靠，反驳竞争假设同样是证据。
- 修正：评估 schema 增加 `action_evidence`，由模型显式给出支撑动作的证据下标；给出无效下标或空列表就不提案。
- 14:00:52 对仍在故障中的集群发起 CLI 调查 `inv-e5072b516d10`，14:02:31 完成，生成提案 `act-cce0ce7032a6`，绑定 3 条证据；14:03:04 人工审批，14:05:07 执行 `verified`，集群恢复。
- 14:06:21 再次注入走告警链路，14:07:45 告警，webhook 建调查 `inv-e81757d2e978`；完成后自动生成提案 `act-e6e1fcfd8a2a`，绑定 5 条证据，状态 `pending_approval`。审批后执行 `verified`，`probe_success=1`，活动告警 0。
- 原始输出：[`08-model-proposal.txt`](outputs/08-model-proposal.txt)、[`08-model-proposal-execution.txt`](outputs/08-model-proposal-execution.txt)、[`09-webhook-model-proposal-execution.txt`](outputs/09-webhook-model-proposal-execution.txt)。

模型提案只到"提案"为止：规则判定为 approval，执行仍要人工批准。

## 8. 复现

前置：集群健康，Prometheus、AlertManager、前端三个 port-forward 在位，`.env` 含数据库、kubeconfig、模型与 webhook token。

```bash
kubectl port-forward -n vigil-shop svc/frontend 8080:80 --address 127.0.0.1
kubectl port-forward -n vigil-monitoring svc/prometheus 9090:9090 --address 127.0.0.1
kubectl port-forward -n vigil-monitoring svc/alertmanager 9093:9093 --address 127.0.0.1
uv run --frozen aiops serve --host 172.20.0.1 --port 8081
```

然后按第 3 节的顺序执行：注入镜像、等待告警与 webhook 记录、`aiops report`、`aiops act`、`aiops approve`、核对 `probe_success` 与首页。

## 9. 复位

运行结束后 `productcatalogservice` 已回到健康 digest，12 个工作负载 1/1，`probe_success=1`，活动告警 0，坏 ReplicaSet 0 副本。webhook、port-forward 由操作者停止；没有执行集群删除。
