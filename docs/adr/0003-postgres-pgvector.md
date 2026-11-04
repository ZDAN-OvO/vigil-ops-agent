---
status: accepted
---

# 复用一个 PostgreSQL 实例保存业务状态、checkpoint 与知识向量

在已经需要关系状态与持久化工作流的前提下，选择 PostgreSQL + pgvector，而不是再引入独立 Chroma 存储，以集中管理业务关联、知识过滤、备份及运维。业务表、官方 checkpoint 表与知识向量保持各自职责；大原始结果以文件引用保存，完整备份还需包含引用文件。此决定基于减少组件和复用关系能力，不意味着 Chroma server 不支持共享访问，也不承诺未经实测的检索性能或企业授权能力。
