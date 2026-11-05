# MS06 证据：Runbook 辅助调查

2026-11-05 首次执行。调查可以查询结构化 runbook，逐条核实后引用或拒绝；runbook 内容不会进入动作证据，也不构成动作授权。

## 条目

[`runbooks/oom-limit.json`](../../runbooks/oom-limit.json) 里有三条：内存限额导致 OOMKilled、服务名解析失败、就绪探针失败。条目是通用排查顺序，与提示词里的通用规则一致，不含服务名、具体限额或场景脚本路径。三条都登记在同一个文件里，`source` 指向 `runbooks/oom-limit.json`。

导入与查看：

```bash
aiops knowledge import --path runbooks
aiops knowledge list
```

## 查询与引用

调查通过 `knowledge.list_runbooks` 查候选，必要时用 `knowledge.get_runbook` 读全文。运行时把 runbook 的观测记为 `knowledge` 来源的证据，但不允许它出现在 `action_evidence` 里；动作仍然只能由集群观测支撑。

模型在 `runbook_uses` 里逐条给出 `runbook_id`、`accepted` 和 `reason`。运行时只接受本次实际查询到的条目，其余记为 `runbook.unknown` 并丢弃。

## 实机运行

先注入 F02 的内存限额故障，`currencyservice` 限额降到 16Mi，容器进入 OOMKilled 重启循环，首页 500。

调查 `inv-4ce0757b47b0` 完成，8 条证据、9 次工具调用、3 次模型调用。结论指出 revision 8 的限额是 16Mi、历史 revision 7 是 128Mi，属于规格回退类故障，并明确写了「重建 Pod 会沿用当前模板、无法修复」。结论末尾引用 `rb-oom-limit v1`，并注明仅作排查顺序参考，未当作已观测事实。

三条 runbook 的处理：

| 条目 | 结果 | 理由 |
|---|---|---|
| `rb-oom-limit` | accepted | 重启原因 OOMKilled、exit 137、restartCount 3，且按该顺序比较了 revision 规格 |
| `rb-dns-resolution` | rejected | 服务名已解析为 ClusterIP，连接被拒绝不是解析失败 |
| `rb-readiness-probe` | rejected | 探针失败是 OOM 崩溃的次生现象，没有探针配置错误的证据 |

调查同时生成了回退提案 `act-d0b364a79ffd`。MS06 不验证执行，操作者拒绝该提案并说明原因，随后由注入器恢复规格：revision 9、128Mi、0 重启、首页 200、探针 1、无活动告警。

## 错误 Context 的反例

再注入一次同类故障，把「有人怀疑是节点磁盘写满导致」放进事件 Context。调查 `inv-2facf6216bc0` 完成，10 条证据、12 次工具调用，结论明确反驳了磁盘假说：事件流里只有 OOMKilled 与探针失败，没有 Evicted、DiskPressure 或 FailedMount，`currencyservice` 日志里也没有磁盘相关错误；同时结论如实写明 Prometheus 不可用、节点磁盘指标没有取到，所以磁盘假说只是「没有事件层面证据」，不是用指标正面排除。Context 只作为待核实线索，没有变成证据，也没有改变动作建议。

这次运行查询过一次 runbook 候选，但没有命中条目（关键词与服务名没有匹配到三条通用条目），因此 `runbook_uses` 为空是允许的；规则只在真正取回候选时要求逐条判定。

## 运行时闸门

第一轮评审发现三处缺口，已经修掉并用测试覆盖：

- 引用未取回的条目不再只是丢掉结构化字段，而是中止调查并写明原因。
- 查询过候选却没有任何判定时同样中止，避免「查了却不表态」。
- 只有 `knowledge` 来源的支撑证据时，结论不会被采纳，记为 inconclusive。

这些规则在 `demos/ms06/outputs/` 的记录之后生效，所以前两条运行记录仍按当时的代码展示；闸门本身由 `tests/test_investigation.py` 的四个用例覆盖。

## 缺口

- 只有三条手工维护的条目，没有版本化审核流程，也没有检索评分或排序依据。
- `runbook_uses` 只记录模型自述的理由，没有对理由本身的交叉核对。
- 只做了一个错误 Context 的反例；`Context` 仍是事件字段，没有独立的核实环节，也没有覆盖正确 Context 被采纳的正例。
- PDF 与历史事故知识不在本里程碑范围。
