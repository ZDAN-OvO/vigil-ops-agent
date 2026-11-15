# 故障目录

每个子目录是一个可注入、可核验、可复位的故障场景：README 写明机制、真值、判定标准和操作步骤，`runs/` 保存逐次运行记录。验收规则见[路线图](../docs/ROADMAP.md#验收规则)。

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

当前每个场景由 `repeat.sh` 驱动，还没有统一的 `inject/verify/cleanup` 接口。
