# 配置开关导致业务请求失败

`vigil-experiment/vigil-config-fixture` 的容器 `app` 有两种配置来源。ConfigMap `vigil-config-fixture` 的 `data.mode` 通过完整目录卷挂载，应用为每次请求读取文件。应用直接读取字面量环境变量 `VIGIL_MODE`。更改其中一个来源即可触发对应告警。

| 查询 | 用途 |
| --- | --- |
| `probe_success{job="vigil-config-probe",signal="gate",source="configmap"}` | ConfigMap 配置预检。把 `source` 换成 `env` 核验环境变量 |
| `probe_success{job="vigil-config-probe",signal="business",source="configmap"}[5m]` | 读取过去五分钟的真实业务样本 |
| `vigil_config_fixture_loaded_mode{job="vigil-config-state",source="configmap",mode="healthy"}` | 应用已读到健康文件。对照抓取和写入时间 |

写入 ConfigMap 后等 kubelet 更新投影文件。写入环境变量后等新模板滚动完成。核对配置预检、加载信号与业务样本，再判定新值生效。

```bash
bash faults/f05-service-config/repeat.sh install
bash faults/f05-service-config/repeat.sh inject configmap
bash faults/f05-service-config/repeat.sh verify configmap
bash faults/f05-service-config/repeat.sh status configmap
bash faults/f05-service-config/repeat.sh restore configmap
```

环境变量场景将命令中的 `configmap` 换为 `env`。一次只触发一个来源。先恢复当前来源并等告警清除，再进入下一次独立场景。

补偿场景执行 `inject configmap compensate`。告警预检持续失败。业务请求在 UTC 分钟数模 5 为 0 的期间失败。策略将候选改为 `worsen` 后，业务请求全部失败。投递告警前，等应用加载故障值满五分钟，再用 `samples configmap` 核对五个业务样本有一个失败。补偿后重复读取，确认恢复到原先的间歇故障。

用 `inject configmap low-margin` 复现不足 50 个百分点的恶化。业务基线为 1/5，`worsen` 时为 2/5。正常模式的 `degraded` 可以构造 5/5 的全失败基线。审批变体使用已登记的普通值 `limited`，该值的业务状态为健康。

`share` 启动第二个 ConfigMap 消费者。核对它就绪后提交共享配置变体的提案。完成取证后运行 `unshare`。
