-- 对照评估：规则基线也会写假设记录。
-- 规则臂与 Agent 臂共用假设与证据关系表，来源必须能区分，否则无法回查结论是
-- 模型推理还是固定规则得出的。

ALTER TABLE hypotheses DROP CONSTRAINT IF EXISTS hypotheses_origin_check;
ALTER TABLE hypotheses ADD CONSTRAINT hypotheses_origin_check
    CHECK (origin IN ('model', 'runbook', 'human', 'rules'));
