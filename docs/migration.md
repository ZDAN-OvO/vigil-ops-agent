# W0 迁移记录

起点：`4790c42427ee012370c0e61d9a59657650936769`。实现分支：`refactor/aiops-foundation`。

迁移前工作区干净。根目录除 `.git/` 外的全部 21 个条目、114 个文件原样移入 `deprecated/`，包括旧 AGENTS/CLAUDE、gitignore、pre-commit、pyright、Python 版本、依赖/锁文件、`.env`、Docker Compose、SQL、文档与 Windows 脚本。

最初 W0 将新项目放入 `aiops/` 并由根目录 Makefile 转发。随后按用户确认的单项目布局提升到仓库根目录：`pyproject.toml`、`uv.lock`、`.python-version`、`.env.example`、`Makefile` 与 `src/`、`tests/`、`env/`、`faults/`、`demos/` 直接位于根目录；文档合并至 `docs/`，保留已有 `docs/agents/`。旧 `aiops/` 容器目录已移除，Python 包仍叫 `aiops`。归档不作为 uv workspace 成员，也不进入新包构建和测试发现。

提升时合并两份 README，项目 Makefile 替换根目录转发包装。原根目录 README/Makefile 与旧虚拟环境备份在 `/tmp/vigil-root-layout-grnq6c4b/`；新虚拟环境在根目录重新创建，避免旧入口脚本的绝对路径失效。该临时备份不是运行依赖。

## 本地配置

旧 `.env` 以前已被 Git 跟踪，此次移至 `deprecated/.env` 并被新忽略规则排除；后续提交应表现为旧跟踪路径删除、本地归档保留，不强制加入归档 `.env`。本次不重写历史、不输出配置内容，也不生成含旧凭据的新模板。

新配置只读取根目录 `.env` 和 `VIGIL_` 环境变量；示例只声明骨架实际使用的字段。数据库与模型配置在接入阶段补充，不能隐式回落到旧配置。

## 核验

迁移前逐文件计算 SHA-256，清单保存在 `/tmp/vigil-w0-archive-manifest.json`（仅相对路径和哈希）；迁移后对全部 114 个文件重新核验。该清单为本次本地核验产物，不是新项目运行依赖。

可复现限制：清单位于临时目录，尚未作为持久证据归档；换机器或清理临时目录后不能仅按本文复现这次迁移前后比对。后续留存迁移证据时需保存可安全公开的路径／哈希清单和操作方法，旧 `.env` 内容仍不得复制进证据包或提交。本轮文档制定只记录该缺口，没有重新迁移或改动归档。

后续以 Git 变更审查确认归档路径，再按根目录 `make check-all` 验证新项目。回退迁移应先检查新改动，再逆向移动，不能用覆盖/删除方式丢弃新工作。
