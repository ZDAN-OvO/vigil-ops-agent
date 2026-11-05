-- 按提案查询待办用于 CLI 展示与评估，补一个索引。

CREATE INDEX IF NOT EXISTS action_followups_proposal_idx
    ON action_followups (proposal_id, created_at);
