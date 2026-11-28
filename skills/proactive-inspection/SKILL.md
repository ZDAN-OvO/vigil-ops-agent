---
name: proactive-inspection
description: 按登记范围巡查工作负载、节点和入口，并核对异常观察。
metadata:
  vigil-inspection-steps:
    - workload-readiness
    - workload-signal
    - node-ready
    - entry-probe
---

# 主动巡检

1. 检查登记工作负载的期望副本、就绪副本和控制器观测代次。
2. 核对工作负载的登记业务指标及样本时间。
3. 检查登记节点的 Ready 状态。
4. 检查登记入口的黑盒探针成功状态。

异常核对从本轮观察和证据引用出发。核对目标身份、样本时间和阈值，区分确实异常与采集失败、样本过期或数据缺失。信息不足时列出待人工核对的证据。采集与补采仅使用登记检查；动作和参数须由处置策略给出。
