"""真实库测试共用的连接串与清理标记。"""

from aiops.config import Settings

DSN = Settings().database_url
# 测试提案的统一标记；清理只删带这个标记的记录，避免误删并发创建的真实提案。
TEST_REASON = "测试：Pod 级状态损坏"
