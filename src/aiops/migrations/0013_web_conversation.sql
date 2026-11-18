-- 页面会话、消息、幂等请求、后台工作和变更事件。
CREATE TABLE conversations (
    id text PRIMARY KEY,
    investigation_id text UNIQUE NOT NULL REFERENCES investigations (id),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    interrupted boolean NOT NULL DEFAULT false
);

CREATE UNIQUE INDEX conversations_single_active ON conversations ((true))
    WHERE finished_at IS NULL;

CREATE TABLE conversation_messages (
    id text PRIMARY KEY,
    conversation_id text NOT NULL REFERENCES conversations (id),
    role text NOT NULL CHECK (role IN ('user', 'agent')),
    kind text NOT NULL CHECK (kind IN ('initial', 'hint', 'conclusion')),
    content text NOT NULL,
    delivery text CHECK (delivery IN ('received', 'delivered', 'unconfirmed')),
    delivered_round integer CHECK (delivered_round > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((delivery IS NOT DISTINCT FROM 'delivered') = (delivered_round IS NOT NULL))
);
CREATE INDEX conversation_messages_order
    ON conversation_messages (conversation_id, created_at, id);

CREATE TABLE web_requests (
    request_id text PRIMARY KEY,
    operation text NOT NULL,
    resource_id text,
    body_hash text NOT NULL,
    response_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE web_work (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversation_id text NOT NULL REFERENCES conversations (id),
    kind text NOT NULL CHECK (kind IN ('start', 'resume')),
    proposal_id text REFERENCES action_proposals (id),
    state text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'running', 'done')),
    instance_id text,
    queued_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    CHECK ((kind = 'resume') = (proposal_id IS NOT NULL)),
    CHECK (state <> 'running' OR instance_id IS NOT NULL)
);

CREATE UNIQUE INDEX web_work_single_active ON web_work (conversation_id)
    WHERE state IN ('queued', 'running');

CREATE TABLE conversation_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversation_id text NOT NULL REFERENCES conversations (id),
    kind text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX conversation_events_cursor ON conversation_events (conversation_id, id);

CREATE TABLE web_serve_instance (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    instance_id text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE investigation_tasks
    ADD COLUMN ready_proposal_id text REFERENCES action_proposals (id),
    ADD COLUMN actions_disabled boolean NOT NULL DEFAULT false;

ALTER TABLE approvals ADD COLUMN card_hash text;
