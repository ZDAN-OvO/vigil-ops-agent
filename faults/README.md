# 故障目录

每个子目录是一个可注入、可核验、可复位的故障场景：README 写明机制、真值、判定标准和操作步骤，`runs/` 保存逐次运行记录。

| 场景 | 目录 | 用途 |
|---|---|---|
| 坏发布 | [`f01-bad-release/`](f01-bad-release/README.md) | 核心场景：定位故障发布，回退恢复 |
| 持续 OOM | [`f02-memory-limit-oom/`](f02-memory-limit-oom/README.md) | 核心场景：识别规格不足，判断处置适用性 |
| Redis 访问链路延迟 | [`f03-redis-latency/`](f03-redis-latency/README.md) | 核心场景：定位链路变化，正确停止 |
| Pod 重建 | [`a01-pod-rebuild/`](a01-pod-rebuild/README.md) | 动作验证：`restart_pod` 的适用性与业务恢复，不计入核心场景分母 |

规则：

- **真值隔离**：真值、脚本和场景命名只在本目录。调查工具只能读取集群对象、Prometheus、runbook 和 `skills/`；webhook 会丢弃 `scenario` 等实验标签；实验镜像使用中性标签和错误文本。
- **注入不等于授权**：注入器能在专用环境制造故障，不代表 Agent 获得任意配置修改权限。
- **复位分开记录**：复位在结果判定之后进行，记录 actor、时间和健康状态，不算 Agent 修复成功。
- **新增场景**：先写明机制、两类互补信号、可接受结论、允许动作和复位方法，再进入验收。不把同一故障换个名字凑数。

## 统一动词与结论行

四个场景的 `repeat.sh` 必须提供同一套动词，评估驱动只认这套接口：

```bash
bash faults/<场景>/repeat.sh preflight   # 健康基线与所有权检查，不改集群
bash faults/<场景>/repeat.sh inject      # 注入并等到故障成立
bash faults/<场景>/repeat.sh verify      # 确认故障仍然成立
bash faults/<场景>/repeat.sh recovered   # 只读检查业务是否已恢复
bash faults/<场景>/repeat.sh cleanup     # 复位到健康基线
```

每个动词的最后一行是机器可读结论：

```
RESULT verb=verify ok=1 probe=0 alert=firing home=500
```

约定：值里不能有空格，缺值写 `unknown`，失败时写 `ok=0 reason=<代码>`。没有这一行、
退出码非零或 `ok=0`，评估驱动都判该步失败。机制的实现仍只写在场景脚本里；共用的结论
行函数在 [`lib/result.sh`](lib/result.sh)。

`aiops lab` 按 `preflight → inject → verify → 调查臂 → recovered → cleanup` 跑一次运行，
任何一步失败都判无效并写明原因，然后照样复位。对照口径与门槛见
[ADR-0013](../docs/adr/0013-comparative-evaluation.md)，用法见
[操作手册](../docs/USAGE.md#9-对照评估)。

## 验收规则

以下规则适用于所有真实环境验收。

- **有效运行**：健康基线成立、故障核验通过、注入与观测记录齐全，才算有效运行。只有独立前提失败（例如注入未生效、基线不健康）才能标为无效，并且要保留理由。
- **分母**：所有有效运行都计入分母。模型失败、预算耗尽、错误判断都不能从分母中剔除；失败运行不会被后来的成功运行覆盖。
- **调查是否正确**：先按 ADR-0013 的机器口径判定（结论词表、必需证据、动作决策），`needs_review` 的条目再人工核对结论原文，并把依据写进运行记录。
- **恢复归属**：Agent、操作者、控制器和实验脚本的动作分开记录。操作者复位和 cleanup 不算 Agent 恢复；动作前业务已经恢复，也不算 Agent 恢复。
- **证据**：PR 证据使用截图或录屏，上传到 PR 评论，覆盖初始、关键中间和最终状态，不提交到仓库，也不放进 PR 正文。命令输出、日志和数据库记录只作补充。本地截图存于已忽略的 `.evidence/`。运行结论写入 `faults/<场景>/runs/<日期>.md`，引用调查 ID 与提案 ID。

## 停止条件

- 同一场景连续 3 次有效运行失败：暂停，给出根因分析与修复方案，等用户确认后继续。
- 预算成为失败主因：按 [ADR-0011](../docs/adr/0011-investigation-budget.md) 的方法重测，等用户确认后再修改默认值。
- 删除表、清理集群资源、强制推送、合并 PR：先取得用户授权。
