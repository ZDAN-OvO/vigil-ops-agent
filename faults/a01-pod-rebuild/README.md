# A01 Pod 重建动作的独立验证用例

状态：2026-11-05 在 `vigil-lab` 完成两轮真实实验，验证了机制本身：临时状态故障跨容器重启持续；在已测的容器重启与 Pod 重建之间，只有 Pod 重建能消除它，且重建前后模板与 ReplicaSet 不变。`rollout restart` 与模板变更没有测试，也不满足本用例的判据，理由见第 1 节。这个用例不进入三个核心故障的分母。同一负载也是 MS05 `restart_pod` 动作链的目标：它带 `vigil.io/discardable=true` 标签，是策略允许重建的条件，动作证据见 [`demos/ms05/`](../../demos/ms05/README.md)。运行记录见 [`runs/2026-11-05.md`](runs/2026-11-05.md)。

## 1. 场景与真值

场景卡见 [MVP A01](../../docs/mvp.md#a01pod-重建动作的独立验证用例)。

- 载体：`vigil-experiment` 里的专用单副本 Deployment `rebuild-demo`，不接入前端，带 `vigil.io/discardable=true` 标签。
- 故障：运行时写坏 Pod 级 `emptyDir` 上的状态文件，应用每次处理 `/work` 都返回 500。
- 预期：容器重启（同一 Pod、restartCount 增加）后故障仍在；删除 Pod 由控制器重建后恢复。
- 未测试的旁路：`rollout restart` 也会新建 Pod，理论上同样能清掉 Pod 级状态。它改动 revision，不满足本用例“模板与 ReplicaSet 不变”的判据，因此没有作为对照运行，文档也不声称它是或不是恢复手段。
- 反向边界：Pod 保持 Ready，排队网络通路正常，所以故障表现为业务请求失败而不是端点摘除；`rollout restart`、滚动更新或修改模板都不属于本用例的恢复方式。

载体放在专用实验命名空间 `vigil-experiment`。脚本创建命名空间时写入 `vigil.io/owner=a01-pod-rebuild`，清理只删除带该标注的命名空间；命名空间已存在但不属于 A01 时直接停止，不接管也不删除。动作策略只在目标同时满足“实验命名空间 + 可丢弃 Pod”时自动允许，其余情况仍要人工审批；清理不影响 `vigil-shop` 的 12 个业务工作负载。

真值只保存在本目录和运行记录中，包括状态文件路径、写坏方式和判据。这些内容不进入调查模型可读范围，访问隔离仍未实现。

## 2. 实验负载

[`app.py`](app.py) 是一个只用标准库的 HTTP 服务。

- `/healthz` 永远返回 200，只表示进程存活，不读取业务状态。
- `/work` 在状态文件内容为 `corrupt` 时返回 500，否则返回 200。
- `/crash` 返回 202 后调用 `os._exit(1)`，用于模拟进程崩溃。

容器内 PID 1 无法被同一 PID 命名空间的进程杀死，因此用 `/crash` 触发容器退出，由 kubelet 按 `restartPolicy: Always` 原地重启容器。liveness 探测刻意只探 `/healthz`，否则 kubelet 会自行重启容器，掩盖“自恢复不成立”这一判断。

构建与导入：

```bash
docker build -t vigil/pod-rebuild-demo:20261105 faults/a01-pod-rebuild
k3d image import vigil/pod-rebuild-demo:20261105 -c vigil-lab
```

- 基础镜像 `python:3.12-slim`。
- 本地镜像 ID `sha256:e5cba3e138b9`，大小 177 MB。
- 镜像只在本地构建和导入，没有推送外部仓库。

## 3. 两类信号

- 平台侧：Pod UID、容器 `restartCount`、`lastState.terminated.reason`、Pod 就绪状态、目标 Pod 的所属 ReplicaSet、Deployment 模板哈希。由 `kubectl` 读取，不从业务响应推断。任一字段读不到就判实验失败，不能用空值互相相等冒充“没有变化”。
- 业务侧：宿主经 `kubectl port-forward` 直接转发到目标 Pod 的 8080 端口，逐秒统计 200、500 和连接失败。转发目标写死为 Pod，Service 只用于选择 Pod，不在数据通路上。本用例不声称覆盖 Service 或 kube-proxy 的路由行为。

两类信号来源不同，不能互相替代。重建成功必须同时满足：新 UID、ReplicaSet 与模板不变、真实的 `/work` 请求在总窗口内回到 200。

## 4. 冻结参数

| 项目 | 取值 | 依据 |
|---|---|---|
| 目标 | `vigil-experiment/rebuild-demo`，单副本 | 专用无状态、数据可丢弃 |
| 故障写入 | `/state/work_state` 内容 `corrupt` | emptyDir 属于 Pod 生命周期 |
| 持续判定 | 观察窗内 100% 请求失败，Pod UID 不变，`restartCount` 不增加 | 排除单次偶发和平台自恢复 |
| 观察窗 | 默认 60 秒，逐秒采样 | 故障轮次实测 60/60、30/30 |
| 容器重启判定 | 同一 UID，`restartCount` 加 1，之后请求仍全部失败 | 证明 K8s 自恢复无效 |
| 恢复判定 | 新 Pod UID、所属 ReplicaSet 不变、模板哈希不变、`/work` 回到 200 | 不允许用模板变更或 `rollout restart` 冒充重建；三个字段任一为空直接判失败 |
| 恢复窗口 | 从删除时刻起 90 秒总时限，超时判失败 | 两轮实测 6 秒、4 秒 |
| 清理判定 | Deployment 与匹配的 Pod 都消失才算完成 | 只看 Deployment 会在 Pod 仍在时误报已清理 |

## 5. 操作步骤

```bash
bash faults/a01-pod-rebuild/repeat.sh deploy            # 部署实验负载并打印基线
bash faults/a01-pod-rebuild/repeat.sh inject            # 写坏状态文件并确认 /work 500
bash faults/a01-pod-rebuild/repeat.sh verify            # 观察窗内确认故障持续
bash faults/a01-pod-rebuild/repeat.sh container-restart # 容器重启后故障仍在
bash faults/a01-pod-rebuild/repeat.sh rebuild           # 操作者删除 Pod，核验新 UID 与恢复
bash faults/a01-pod-rebuild/repeat.sh cleanup           # 删除实验负载
```

`rebuild` 由操作者执行，只用于证明机制，不是 Agent 处置，也不能计入 Agent 恢复成功。脚本依赖宿主机 `kubectl`、`curl`、`python3`，默认用 8082 端口做 port-forward。

## 6. 运行结果（2026-11-05，迁移前在 `vigil-shop` 取得）

| 轮次 | 关键事实 |
|---|---|
| 第 1 轮 | 注入后 60 秒观察窗 60/60 请求失败，UID `7976e2fe…`，0 重启；容器重启后同一 UID、`restartCount` 1、20/20 仍失败；删除 Pod 后新 UID `d49afd7e…`，ReplicaSet 与模板哈希不变，6 秒恢复 |
| 第 2 轮 | 注入后 30 秒观察窗 30/30 失败；容器重启后 `restartCount` 1、20/20 仍失败；重建后新 UID `0d3524c6…`，模板不变，4 秒恢复 |

- 模板哈希两轮均为 `5427399d73722fda540480d8127ec4bdbb9d3e1dba750007aefebd7a9cc67e80`，目标 Pod 的所属 ReplicaSet 均为 `d62c8fb9-198c-4ab7-a549-a93788930e8f`。
- 容器崩溃后的终止状态为 `reason=Error`、`exitCode=1`，Pod 保持不变。
- 清理后业务基线恢复：12 个 Deployment、`probe_success=1`、活动告警 0、`rebuild-demo` 的 Deployment 与 Service 都不存在。
- 这两轮在负载迁到 `vigil-experiment` 之前取得，冻结目标与脚本默认命名空间在那之后改成了 `vigil-experiment`。迁移后的自动执行与清理证据见 [`demos/ms08/`](../../demos/ms08/README.md)。

## 7. 缺口

- `restart_pod` 动作链已在 MS05 实现，调查侧也能生成该提案；证据见 [`demos/ms05/`](../../demos/ms05/README.md)。
- 实验负载没有接入 Prometheus 采集和告警，两类信号靠 `kubectl` 与宿主请求循环取得。是否需要告警入口由 MS05 的完整链路决定。
- 真值访问隔离仍未实现。
- 运行依赖宿主 port-forward 和本地镜像导入，换机器要重新构建镜像。
- 第一版脚本有三处判据缺陷，由独立评审查出后修正：空 UID 与空 `restartCount` 互相相等会被当作“未变化”，ReplicaSet 取的是标签列表第一条而不是目标 Pod 的归属，恢复窗口没有总时限。修正后重跑了两轮，上面的记录来自修正版。
