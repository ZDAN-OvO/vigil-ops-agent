# 故障目录

完整目标为资源、依赖、配置发布、网络四类共 12 个可复现场景，每个场景提供 `inject/verify/cleanup`。机制和验收以 [MVP 场景卡](../docs/mvp.md#5-场景卡)为准。

当前状态：F01 坏发布、F02 持续 OOM 和 F03 Redis 访问链路延迟已完成实验级验证。F01 的场景、实验镜像改法、冻结参数、操作步骤和四次有效运行记录在 [`f01-bad-release/`](f01-bad-release/README.md)；F02 的目标、限额、脚本和两轮运行结果在 [`f02-memory-limit-oom/`](f02-memory-limit-oom/README.md)，对应 Agent 证据见 [`demos/ms03/`](../demos/ms03/README.md)；F03 的代理、Service 路由、脚本和一次运行结果在 [`f03-redis-latency/`](f03-redis-latency/README.md)，对应 Agent 证据见 [`demos/ms04/`](../demos/ms04/README.md)。实验镜像已改为中性标签和中性错误文本，业务响应与日志不再包含场景名。A01 已完成机制实验：专用构造的临时状态故障跨容器重启持续，在已测动作之间只有 Pod 重建能消除它，重建前后模板与 ReplicaSet 不变，见 [`a01-pod-rebuild/`](a01-pod-rebuild/README.md)。仓库内只有实验驱动脚本 `repeat.sh`，统一的 `inject/verify/cleanup` 接口、真值访问隔离和清理 actor 记录仍待实现。

首个切片为 F01 坏发布：实验镜像能启动但关键业务请求失败，经调查和受控回退验证真实业务恢复。F02 为持续内存限额不足的 OOM，必须正确判断 Pod 重建无法改变故障规格。F03 为实验代理保持的 Redis 访问链路延迟，不称为 Redis 内部阻塞；现有动作不适用时停止。

A01 是专用无状态工作负载的独立 Pod 重建验证用例，不拼入核心三场景分母。临时故障必须经实验证明跨容器重启持续、Pod 重建消除，不能把 Kubernetes 自恢复算成 Agent 成功。机制实验已于 2026-11-05 通过两轮运行，`restart_pod` 动作链同日在同一负载上跑通，证据见 [`demos/ms05/`](../demos/ms05/README.md)。

其余 12 场景覆盖项逐步核对真实业务能力，不将同一故障换名凑数。“回退上一版”是动作，不是故障类型；Redis 不应被假定有应用连接池，磁盘注入需要隔离卷。

注入器可以在专用环境制造故障，不意味着 Agent 获得任意配置修改权限。场景真值、脚本和命名不进入调查模型可读范围；当前尚未实现该访问隔离，接入调查前必须补上。`cleanup` 属于结果判定后的环境复位，记录 actor、时间和健康状态，不计 Agent 修复成功。
