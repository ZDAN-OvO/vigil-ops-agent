-- 单 Agent 工具循环的任务记录、观测关系、动作意图与请求账本。
-- 只新增，不改历史迁移；旧调查与旧提案保持可读。

-- 分阶段预算：调查阶段沿用 budget_wall_clock_seconds，动作阶段单独累计。
ALTER TABLE investigations
    ADD COLUMN IF NOT EXISTS budget_action_seconds integer NOT NULL DEFAULT 450;
ALTER TABLE investigations
    ADD CONSTRAINT investigations_budget_action_positive CHECK (budget_action_seconds > 0);

-- 假设：反驳条件、修订理由和版本；假设不能删除，更新要留历史。
ALTER TABLE hypotheses
    ADD COLUMN IF NOT EXISTS refutation_criteria text[] NOT NULL DEFAULT '{}';
ALTER TABLE hypotheses
    ADD COLUMN IF NOT EXISTS revision integer NOT NULL DEFAULT 1;
ALTER TABLE hypotheses
    ADD COLUMN IF NOT EXISTS update_reason text NOT NULL DEFAULT '';

CREATE TABLE hypothesis_revisions (
    id text PRIMARY KEY,
    hypothesis_id text NOT NULL REFERENCES hypotheses (id) ON DELETE RESTRICT,
    revision integer NOT NULL CHECK (revision >= 1),
    statement text NOT NULL,
    expected_observations text[] NOT NULL DEFAULT '{}',
    refutation_criteria text[] NOT NULL DEFAULT '{}',
    status text NOT NULL CHECK (
        status IN ('pending', 'supported', 'refuted', 'inconclusive')
    ),
    update_reason text NOT NULL,
    actor text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (hypothesis_id, revision)
);

-- 观测：一次工具调用的原始结果只存一份，可被多个假设引用。
CREATE TABLE observations (
    id text PRIMARY KEY,
    investigation_id text NOT NULL REFERENCES investigations (id) ON DELETE RESTRICT,
    tool_call_id text NOT NULL REFERENCES tool_calls (id) ON DELETE RESTRICT,
    source text NOT NULL,
    independence_key text NOT NULL,
    summary text NOT NULL,
    raw_ref text NOT NULL DEFAULT '',
    status text NOT NULL CHECK (status IN ('ok', 'empty', 'error')),
    truncated boolean NOT NULL DEFAULT false,
    time_start timestamptz NOT NULL,
    time_end timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (investigation_id, tool_call_id)
);

CREATE TABLE hypothesis_evidence (
    id text PRIMARY KEY,
    investigation_id text NOT NULL REFERENCES investigations (id) ON DELETE RESTRICT,
    hypothesis_id text NOT NULL REFERENCES hypotheses (id) ON DELETE RESTRICT,
    observation_id text NOT NULL REFERENCES observations (id) ON DELETE RESTRICT,
    relation text NOT NULL CHECK (relation IN ('supporting', 'refuting', 'neutral')),
    rationale text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (hypothesis_id, observation_id)
);

CREATE INDEX hypothesis_evidence_observation_idx
    ON hypothesis_evidence (observation_id);
CREATE INDEX observations_investigation_idx ON observations (investigation_id, created_at);

-- 动作意图：目的、绑定假设、预测与观察条件。
ALTER TABLE action_proposals
    ADD COLUMN IF NOT EXISTS purpose text NOT NULL DEFAULT 'remediation';
ALTER TABLE action_proposals
    ADD CONSTRAINT action_proposals_purpose_check CHECK (
        purpose IN ('diagnostic', 'mitigation', 'remediation')
    );
ALTER TABLE action_proposals
    ADD COLUMN IF NOT EXISTS hypothesis_id text REFERENCES hypotheses (id) ON DELETE RESTRICT;
ALTER TABLE action_proposals
    ADD COLUMN IF NOT EXISTS prediction text NOT NULL DEFAULT '';
ALTER TABLE action_proposals
    ADD COLUMN IF NOT EXISTS observation_conditions jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE action_proposals
    ADD COLUMN IF NOT EXISTS observation_window_seconds integer;

-- 工具请求账本：业务副作用的唯一凭据，重放同一请求时先读这里。
CREATE TABLE tool_requests (
    investigation_id text NOT NULL REFERENCES investigations (id) ON DELETE RESTRICT,
    tool_call_id text NOT NULL,
    tool text NOT NULL,
    arguments jsonb NOT NULL DEFAULT '{}'::jsonb,
    state text NOT NULL CHECK (
        state IN (
            'running', 'observed', 'failed',
            'action_pending', 'action_executed', 'rejected'
        )
    ),
    attempt integer NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    result jsonb,
    result_ref text,
    error_code text,
    proposal_id text REFERENCES action_proposals (id) ON DELETE RESTRICT,
    action_attempt_id text REFERENCES action_attempts (id) ON DELETE RESTRICT,
    started_at timestamptz NOT NULL,
    finished_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (investigation_id, tool_call_id)
);

CREATE INDEX tool_requests_proposal_idx ON tool_requests (proposal_id);

-- 任务运行记录：phase 表示运行阶段，outcome 表示业务/任务结果。
CREATE TABLE investigation_tasks (
    investigation_id text PRIMARY KEY REFERENCES investigations (id) ON DELETE RESTRICT,
    phase text NOT NULL CHECK (phase IN ('running', 'waiting_approval', 'terminal')),
    outcome text CHECK (
        outcome IN ('recovered', 'diagnosis_only', 'inconclusive', 'needs_human', 'aborted')
    ),
    termination_code text,
    proposal_id text REFERENCES action_proposals (id) ON DELETE RESTRICT,
    runtime_version text NOT NULL,
    used_model_calls integer NOT NULL DEFAULT 0 CHECK (used_model_calls >= 0),
    used_tool_calls integer NOT NULL DEFAULT 0 CHECK (used_tool_calls >= 0),
    investigation_seconds double precision NOT NULL DEFAULT 0
        CHECK (investigation_seconds >= 0),
    action_seconds double precision NOT NULL DEFAULT 0 CHECK (action_seconds >= 0),
    no_progress_rounds integer NOT NULL DEFAULT 0 CHECK (no_progress_rounds >= 0),
    calendar_started_at timestamptz NOT NULL,
    paused_at timestamptz,
    finished_at timestamptz,
    cancel_requested_at timestamptz,
    recovered_at timestamptz,
    lease_owner text,
    lease_expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT investigation_tasks_terminal_has_outcome CHECK (
        phase <> 'terminal' OR outcome IS NOT NULL
    ),
    CONSTRAINT investigation_tasks_running_has_no_outcome CHECK (
        phase = 'terminal' OR outcome IS NULL
    ),
    CONSTRAINT investigation_tasks_terminal_has_code CHECK (
        phase <> 'terminal' OR termination_code IS NOT NULL
    ),
    CONSTRAINT investigation_tasks_wait_have_proposal CHECK (
        phase <> 'waiting_approval' OR proposal_id IS NOT NULL
    )
);

CREATE INDEX investigation_tasks_phase_idx ON investigation_tasks (phase, updated_at);

-- 旧证据回填：同一 tool_call 只生成一条观测，关系按原 hypothesis/relation 迁移。
INSERT INTO observations (
    id, investigation_id, tool_call_id, source, independence_key, summary, raw_ref,
    status, truncated, time_start, time_end, created_at
)
SELECT DISTINCT ON (e.tool_call_id)
       'obs-' || e.id,
       e.investigation_id,
       e.tool_call_id,
       e.source,
       e.independence_key,
       e.observation,
       e.raw_ref,
       'ok',
       false,
       e.time_start,
       e.time_end,
       e.created_at
  FROM evidence e
 ORDER BY e.tool_call_id, e.created_at, e.id;

INSERT INTO hypothesis_evidence (
    id, investigation_id, hypothesis_id, observation_id, relation, rationale, created_at
)
SELECT 'he-' || e.id,
       e.investigation_id,
       e.hypothesis_id,
       o.id,
       e.relation,
       '旧证据迁移：关系来自 evidence.relation',
       e.created_at
  FROM evidence e
  JOIN observations o ON o.tool_call_id = e.tool_call_id
ON CONFLICT (hypothesis_id, observation_id) DO NOTHING;
