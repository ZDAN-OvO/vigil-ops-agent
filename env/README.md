# 本地实验环境（待实现）

目标为 k3d 单集群、Online Boutique 五个选定组件与可观测栈。W0 没有安装工具或创建集群。

W1 必须先验证五服务依赖闭包；frontend/checkout 可能需要裁剪后端调用、补齐依赖或采用官方支持的配置。记录实际可工作的依赖与镜像版本，再提交 manifest。不能用没有业务流量的 Pod Ready 代替应用可用性。

观测栈目标：Prometheus、AlertManager、Loki、Tempo、OTel Collector，以及实际需要的日志/集群指标采集器。记录整个栈的实测 RSS、负载和启动耗时；此前的内存/耗时数字只是估计。

AIOps 的 PG、调度器与执行恢复进程应避免与故障目标共生命周期。具体进程部署在切片验证后确定。
