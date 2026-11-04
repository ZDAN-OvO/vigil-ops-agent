-- 调查链路最小业务表：事件、调查、假设、工具调用、证据、审计。
-- 大结果不入库，只存 runtime 下的文件引用。

CREATE TABLE incidents (
    id text PRIMARY KEY,
    environment text NOT NULL,
    cluster text NOT NULL,
    namespace text NOT NULL,
    service text NOT NULL,
    trigger text NOT NULL CHECK (trigger IN ('alert', 'manual', 'scheduled')),
    symptoms text NOT NULL,
    time_start timestamptz NOT NULL,
    time_end timestamptz NOT NULL,
    alert_ids text[] NOT NULL DEFAULT '{}',
    context text[] NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT incidents_time_order CHECK (time_end > time_start)
);

CREATE TABLE investigations (
    id text PRIMARY KEY,
    incident_id text NOT NULL REFERENCES incidents (id) ON DELETE RESTRICT,
    parent_run_id text REFERENCES investigations (id) ON DELETE RESTRICT,
    status text NOT NULL CHECK (
        status IN ('pending', 'running', 'completed', 'inconclusive', 'aborted')
    ),
    model text NOT NULL,
    policy_version text NOT NULL,
    budget_tool_calls integer NOT NULL CHECK (budget_tool_calls > 0),
    budget_model_calls integer NOT NULL CHECK (budget_model_calls > 0),
    budget_wall_clock_seconds integer NOT NULL CHECK (budget_wall_clock_seconds > 0),
    used_tool_calls integer NOT NULL DEFAULT 0 CHECK (used_tool_calls >= 0),
    used_model_calls integer NOT NULL DEFAULT 0 CHECK (used_model_calls >= 0),
    conclusion text,
    termination_reason text,
    started_at timestamptz NOT NULL,
    finished_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT investigations_finished_after_started CHECK (
        finished_at IS NULL OR finished_at >= started_at
    ),
    CONSTRAINT investigations_completed_has_conclusion CHECK (
        status <> 'completed' OR conclusion IS NOT NULL
    ),
    CONSTRAINT investigations_terminal_is_finished CHECK (
        status NOT IN ('completed', 'inconclusive', 'aborted') OR finished_at IS NOT NULL
    )
);

CREATE TABLE hypotheses (
    id text PRIMARY KEY,
    investigation_id text NOT NULL REFERENCES investigations (id) ON DELETE RESTRICT,
    parent_id text REFERENCES hypotheses (id) ON DELETE RESTRICT,
    statement text NOT NULL,
    expected_observations text[] NOT NULL DEFAULT '{}',
    origin text NOT NULL CHECK (origin IN ('model', 'runbook', 'human')),
    status text NOT NULL CHECK (status IN ('pending', 'supported', 'refuted', 'inconclusive')),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tool_calls (
    id text PRIMARY KEY,
    investigation_id text NOT NULL REFERENCES investigations (id) ON DELETE RESTRICT,
    tool text NOT NULL,
    parameters jsonb NOT NULL,
    time_start timestamptz NOT NULL,
    time_end timestamptz NOT NULL,
    started_at timestamptz NOT NULL,
    finished_at timestamptz,
    error_code text,
    retry_count integer NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    truncated boolean NOT NULL DEFAULT false,
    raw_ref text,
    CONSTRAINT tool_calls_time_order CHECK (time_end > time_start)
);

CREATE TABLE evidence (
    id text PRIMARY KEY,
    investigation_id text NOT NULL REFERENCES investigations (id) ON DELETE RESTRICT,
    hypothesis_id text NOT NULL REFERENCES hypotheses (id) ON DELETE RESTRICT,
    tool_call_id text NOT NULL REFERENCES tool_calls (id) ON DELETE RESTRICT,
    source text NOT NULL,
    independence_key text NOT NULL,
    observation text NOT NULL,
    relation text NOT NULL CHECK (relation IN ('supporting', 'refuting', 'neutral')),
    raw_ref text NOT NULL,
    time_start timestamptz NOT NULL,
    time_end timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE audit_events (
    id text PRIMARY KEY,
    correlation_id text NOT NULL,
    actor text NOT NULL,
    event text NOT NULL,
    occurred_at timestamptz NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX investigations_incident_idx ON investigations (incident_id);
CREATE INDEX hypotheses_investigation_idx ON hypotheses (investigation_id);
CREATE INDEX tool_calls_investigation_idx ON tool_calls (investigation_id);
CREATE INDEX evidence_investigation_idx ON evidence (investigation_id);
CREATE INDEX evidence_tool_call_idx ON evidence (tool_call_id);
CREATE INDEX audit_events_correlation_idx ON audit_events (correlation_id, occurred_at);
