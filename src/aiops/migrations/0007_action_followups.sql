-- 执行异常停止后的人工待办；与执行记录关联，单独跟踪解决状态。

CREATE TABLE action_followups (
    id text PRIMARY KEY,
    proposal_id text NOT NULL REFERENCES action_proposals (id) ON DELETE RESTRICT,
    attempt_id text REFERENCES action_attempts (id) ON DELETE RESTRICT,
    cluster text NOT NULL,
    namespace text NOT NULL,
    target_kind text NOT NULL,
    target_name text NOT NULL,
    outcome text NOT NULL CHECK (outcome IN ('no_improvement', 'worsened', 'unknown')),
    reason text NOT NULL,
    evidence jsonb NOT NULL DEFAULT '{}',
    status text NOT NULL CHECK (status IN ('open', 'resolved')),
    created_at timestamptz NOT NULL,
    resolved_at timestamptz,
    resolved_by text,
    resolution text
);

CREATE UNIQUE INDEX action_followups_attempt_key
    ON action_followups (attempt_id) WHERE attempt_id IS NOT NULL;

CREATE INDEX action_followups_status_idx
    ON action_followups (status, created_at);
