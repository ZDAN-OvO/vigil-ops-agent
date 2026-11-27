# 单实例无法处理两笔并发交易

网关从 headless Service 解析可用实例。每 15 秒，黑盒探针请求网关的 `/business`，网关同时提交两笔交易。单实例只允许一个并发请求，另一笔返回 429。双实例各承接一笔，批次返回 200。请求在 Pod 中处理约 500 毫秒。

| 待核对字段 | 故障 | 恢复 |
| --- | --- | --- |
| `spec.replicas` | 1 | 2 |
| `vigil_replica_fixture_endpoints{job="vigil-replica-state"}` | 1 | 2 |
| `vigil_replica_fixture_rejected{job="vigil-replica-state"}` | 1 | 0 |
| `probe_success{job="vigil-replica-probe"}` | 0 | 1 |

`VigilReplicaCapacityDegraded` 在探针连续失败 1 分钟后触发。DNS 解析失败或网络错误也会使业务请求失败，只有 429 拒绝才能证明容量已饱和。

```bash
bash faults/f04-replica-capacity/repeat.sh install
bash faults/f04-replica-capacity/repeat.sh inject
bash faults/f04-replica-capacity/repeat.sh verify
bash faults/f04-replica-capacity/repeat.sh status
bash faults/f04-replica-capacity/repeat.sh restore
```

等探针和 DNS 更新后运行 `verify`。自动模式下先暂缓告警投递，采集故障状态后解除 silence。待 Vigil 完成观察并采集写入审计后，使用独立操作者复位。运行步骤见 [真实集群验收](../../env/README.md#11-受控自动修复验收)。
