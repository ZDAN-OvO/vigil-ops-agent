# Vigil

Linux + uv，Python。文档、注释、提交信息用简体中文，标识符用英文。

主要结构：

```text
.
├── src/aiops/        # 应用源码；webhook 接入与调查、处置（命令行入口正在重做）
│   ├── agent/       # 调查、取证与只读委派
│   ├── actions/     # 提案、授权、执行与验证
│   ├── tools/       # 集群与监控查询
│   └── migrations/  # 数据库版本变更
├── tests/           # 自动化测试
├── scripts/         # 开发与接入检查
├── skills/          # Agent 排查方法
├── env/             # 实验环境清单与搭建、删除步骤
├── faults/          # 故障机制、注入、复位与验收规则
├── Makefile         # 安装依赖、格式、lint 与测试
├── README.md        # 产品介绍与启动
├── ARCHITECTURE.md  # 模块关系与架构约束
├── GLOSSARY.md      # 领域术语
├── docs/
│   ├── USAGE.md     # 配置、使用流程与排障
│   └── adr/         # 决策背景、取舍与后果；后续决策注明取代关系
└── deprecated/      # 旧项目封存；新代码不导入、不自动加载
```

现行说明随实现更新，各文档只维护自己的主题。阶段计划在任务收尾后删除；实测证据在功能完成后统一整理，不随每次运行提交。

## 协作底线

- 并行写入用独立 worktree，先对齐契约。目标分支走 PR；合并、auto-merge、破坏性清理须明确授权。
- 收尾运行 `make check-all`；基础设施与写操作另需真实环境证据。密钥仅存本地。
- 任务用本仓库 GitHub Issues 跟踪；创建、评论、关闭须任务授权。
