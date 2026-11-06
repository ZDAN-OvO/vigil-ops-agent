-- 规则基线提案与模型提案、人工提案在库里可区分。
ALTER TABLE action_proposals DROP CONSTRAINT IF EXISTS action_proposals_origin_check;
ALTER TABLE action_proposals ADD CONSTRAINT action_proposals_origin_check
    CHECK (origin IN ('operator', 'model', 'rules'));
