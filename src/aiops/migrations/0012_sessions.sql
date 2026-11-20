-- 既有调查、动作和调查租约原样保留。新调查在创建时绑定一轮会话。
CREATE TABLE sessions (
    id text PRIMARY KEY,
    source text NOT NULL CHECK (source IN ('user', 'alert')),
    title text NOT NULL,
    group_key text,
    state text NOT NULL DEFAULT 'idle'
        CHECK (state IN ('idle', 'working', 'awaiting_approval', 'interrupted', 'closed')),
    last_seq bigint NOT NULL DEFAULT 0,
    lease_owner text,
    lease_generation bigint NOT NULL DEFAULT 0,
    lease_expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    closed_at timestamptz,
    CHECK ((source = 'alert') = (group_key IS NOT NULL)),
    CHECK ((state = 'closed') = (closed_at IS NOT NULL)),
    CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL))
);

CREATE UNIQUE INDEX sessions_open_group_key
    ON sessions (group_key) WHERE group_key IS NOT NULL AND closed_at IS NULL;
CREATE INDEX sessions_updated_idx ON sessions (updated_at DESC, id DESC);

CREATE TABLE session_turns (
    id text PRIMARY KEY,
    session_id text NOT NULL REFERENCES sessions (id),
    lane text NOT NULL CHECK (lane IN ('main', 'side')),
    state text NOT NULL
        CHECK (state IN ('queued', 'running', 'awaiting_approval', 'completed', 'aborted')),
    checkpoint_thread_id text NOT NULL,
    first_input_id text NOT NULL,
    lease_owner text,
    lease_generation bigint NOT NULL DEFAULT 0,
    lease_expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    UNIQUE (session_id, id),
    CHECK (lane = 'main' OR state <> 'awaiting_approval'),
    CHECK ((lane = 'main') = (checkpoint_thread_id = session_id)),
    CHECK (lane = 'side' OR lease_owner IS NULL),
    CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL)),
    CHECK ((state IN ('completed', 'aborted')) = (finished_at IS NOT NULL))
);

CREATE UNIQUE INDEX session_main_active
    ON session_turns (session_id) WHERE lane = 'main' AND finished_at IS NULL;
CREATE UNIQUE INDEX session_side_active
    ON session_turns (session_id) WHERE lane = 'side' AND finished_at IS NULL;

CREATE TABLE session_events (
    session_id text NOT NULL REFERENCES sessions (id),
    seq bigint NOT NULL,
    ts timestamptz NOT NULL DEFAULT clock_timestamp(),
    turn_id text,
    type text NOT NULL,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    PRIMARY KEY (session_id, seq),
    FOREIGN KEY (session_id, turn_id) REFERENCES session_turns (session_id, id)
);

CREATE TABLE session_inputs (
    id text PRIMARY KEY,
    session_id text NOT NULL REFERENCES sessions (id),
    queued_seq bigint NOT NULL,
    text text NOT NULL,
    turn_id text,
    delivered_step integer,
    checkpoint_id text,
    UNIQUE (session_id, id),
    UNIQUE (session_id, queued_seq),
    FOREIGN KEY (session_id, queued_seq) REFERENCES session_events (session_id, seq),
    FOREIGN KEY (session_id, turn_id) REFERENCES session_turns (session_id, id),
    CHECK ((delivered_step IS NULL) = (checkpoint_id IS NULL)),
    CHECK (checkpoint_id IS NULL OR turn_id IS NOT NULL)
);

ALTER TABLE session_turns ADD FOREIGN KEY (session_id, first_input_id)
    REFERENCES session_inputs (session_id, id);
CREATE INDEX session_inputs_pending_idx
    ON session_inputs (session_id, queued_seq) WHERE checkpoint_id IS NULL;

ALTER TABLE investigations ADD COLUMN session_turn_id text UNIQUE REFERENCES session_turns (id);
