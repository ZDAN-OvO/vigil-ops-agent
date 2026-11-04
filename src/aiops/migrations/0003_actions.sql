-- 动作提案、审批与执行记录；执行意图先落库再发外部请求。

CREATE TABLE action_proposals (
    id text PRIMARY KEY,
    investigation_id text REFERENCES investigations (id) ON DELETE RESTRICT,
    origin text NOT NULL CHECK (origin IN ('operator', 'model')),
    kind text NOT NULL CHECK (kind IN ('restart_pod', 'scale_replicas', 'rollout_undo')),
    cluster text NOT NULL,
    namespace text NOT NULL,
    target_kind text NOT NULL,
    target_name text NOT NULL,
    target_uid text NOT NULL,
    target_resource_version text NOT NULL,
    parameters jsonb NOT NULL,
    evidence_ids text[] NOT NULL DEFAULT '{}',
    idempotency_key text NOT NULL UNIQUE,
    reason text NOT NULL,
    status text NOT NULL CHECK (
        status IN (
            'pending_approval', 'approved', 'rejected', 'denied',
            'executing', 'succeeded', 'failed', 'unknown'
        )
    ),
    risk_disposition text NOT NULL CHECK (risk_disposition IN ('auto', 'approval', 'deny')),
    risk_reasons text[] NOT NULL,
    policy_version text NOT NULL,
    created_at timestamptz NOT NULL
);

CREATE TABLE approvals (
    id text PRIMARY KEY,
    proposal_id text NOT NULL REFERENCES action_proposals (id) ON DELETE RESTRICT,
    proposal_hash text NOT NULL,
    actor text NOT NULL,
    decision text NOT NULL CHECK (decision IN ('approved', 'rejected')),
    reason text NOT NULL,
    decided_at timestamptz NOT NULL,
    expires_at timestamptz
);

CREATE TABLE action_attempts (
    id text PRIMARY KEY,
    proposal_id text NOT NULL UNIQUE REFERENCES action_proposals (id) ON DELETE RESTRICT,
    status text NOT NULL CHECK (
        status IN ('intent', 'executing', 'verified', 'failed', 'unknown')
    ),
    idempotency_key text NOT NULL,
    snapshot jsonb NOT NULL,
    request jsonb NOT NULL,
    result jsonb,
    verification jsonb,
    started_at timestamptz NOT NULL,
    finished_at timestamptz
);

CREATE TABLE action_locks (
    target_key text PRIMARY KEY,
    proposal_id text NOT NULL,
    acquired_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX action_proposals_status_idx ON action_proposals (status, created_at);
