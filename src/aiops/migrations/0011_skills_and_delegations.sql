-- 技能、委派调查与简化假设模型。
-- 调研方法与 Skill 版本进 investigations；委派调查单独成表；假设不再保留版本历史。

ALTER TABLE investigations
    ADD COLUMN IF NOT EXISTS skills jsonb NOT NULL DEFAULT '[]'::jsonb;

-- 告警名用于 Skill 的信号触发；指纹仍保存在 alert_ids。
ALTER TABLE incidents
    ADD COLUMN IF NOT EXISTS alert_names text[] NOT NULL DEFAULT '{}';

CREATE TABLE delegations (
    id text PRIMARY KEY,
    investigation_id text NOT NULL REFERENCES investigations (id) ON DELETE RESTRICT,
    objective text NOT NULL,
    status text NOT NULL CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
    result_summary text NOT NULL DEFAULT '',
    skills text[] NOT NULL DEFAULT '{}',
    started_at timestamptz NOT NULL,
    finished_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT delegations_finished_after_started CHECK (
        finished_at IS NULL OR finished_at >= started_at
    )
);

CREATE INDEX delegations_investigation_idx ON delegations (investigation_id, started_at);

-- 观测不再外键引用旧工具表；工具账本以 tool_requests 为准。
ALTER TABLE observations DROP CONSTRAINT IF EXISTS observations_tool_call_id_fkey;
ALTER TABLE observations
    ADD COLUMN IF NOT EXISTS delegation_id text REFERENCES delegations (id) ON DELETE RESTRICT;
CREATE INDEX observations_delegation_idx ON observations (delegation_id);

-- 假设只保留陈述、状态、父子关系与更新理由。
ALTER TABLE hypotheses DROP COLUMN IF EXISTS expected_observations;
ALTER TABLE hypotheses DROP COLUMN IF EXISTS refutation_criteria;
ALTER TABLE hypotheses DROP COLUMN IF EXISTS revision;

DROP TABLE IF EXISTS hypothesis_revisions;
DROP TABLE IF EXISTS evidence;
DROP TABLE IF EXISTS tool_calls;
