ALTER TABLE sessions DROP CONSTRAINT sessions_source_check;
ALTER TABLE sessions ADD CHECK (source IN ('user', 'alert', 'inspection'));
ALTER TABLE sessions DROP CONSTRAINT sessions_title_origin_check;
ALTER TABLE sessions ADD CHECK (title_origin IN ('pending', 'auto', 'user', 'alert', 'inspection'));
CREATE INDEX sessions_inspection_created_idx ON sessions (created_at DESC, id DESC)
    WHERE source = 'inspection';

CREATE TABLE inspection_schedules (
    cluster text PRIMARY KEY,
    next_at timestamptz NOT NULL
);

CREATE TABLE inspection_runs (
    id text PRIMARY KEY,
    session_id text NOT NULL UNIQUE REFERENCES sessions (id),
    cluster text NOT NULL REFERENCES inspection_schedules (cluster),
    scheduled_at timestamptz NOT NULL,
    deadline timestamptz NOT NULL,
    owner text NOT NULL,
    generation bigint GENERATED ALWAYS AS IDENTITY,
    lease_expires_at timestamptz NOT NULL,
    sop_hash text NOT NULL,
    status text NOT NULL CHECK (status IN ('running', 'completed', 'timed_out', 'interrupted', 'failed')),
    reason text,
    finished_at timestamptz,
    result jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(result) = 'object'),
    checks jsonb NOT NULL CHECK (jsonb_typeof(checks) = 'array'),
    UNIQUE (cluster, scheduled_at),
    CHECK ((status = 'running') = (finished_at IS NULL)),
    CHECK (deadline > scheduled_at)
);

CREATE UNIQUE INDEX inspection_active_cluster ON inspection_runs (cluster)
    WHERE status = 'running';

CREATE TABLE inspection_observations (
    id text PRIMARY KEY,
    run_id text NOT NULL REFERENCES inspection_runs (id),
    ordinal integer NOT NULL,
    check_id text NOT NULL,
    target jsonb NOT NULL CHECK (jsonb_typeof(target) = 'object'),
    status text NOT NULL CHECK (status IN ('healthy', 'abnormal', 'insufficient')),
    reason text,
    sampled_at timestamptz,
    evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence) = 'object'),
    evidence_refs jsonb NOT NULL CHECK (jsonb_typeof(evidence_refs) = 'array'),
    recorded_order bigint GENERATED ALWAYS AS IDENTITY,
    initial boolean NOT NULL
);

CREATE UNIQUE INDEX inspection_initial_observation ON inspection_observations (run_id, ordinal)
    WHERE initial;
CREATE INDEX inspection_recent_observation ON inspection_observations
    (run_id, ordinal, recorded_order DESC);
