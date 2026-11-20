-- 保留历史记录，由操作者通过切换流程判定遗留动作。
ALTER TABLE action_proposals DROP CONSTRAINT action_proposals_status_check;
ALTER TABLE action_proposals ADD CONSTRAINT action_proposals_status_check CHECK (
    status IN (
        'pending_approval', 'approved', 'rejected', 'denied',
        'executing', 'succeeded', 'failed', 'unknown', 'expired', 'voided'
    )
);
ALTER TABLE action_attempts DROP CONSTRAINT action_attempts_status_check;
ALTER TABLE action_attempts ADD CONSTRAINT action_attempts_status_check CHECK (
    status IN ('intent', 'executing', 'verified', 'failed', 'unknown', 'voided')
);

ALTER TABLE action_proposals
    ADD COLUMN session_id text,
    ADD COLUMN session_turn_id text,
    ADD COLUMN published_seq bigint,
    ADD CHECK ((session_id IS NULL) = (session_turn_id IS NULL)),
    ADD FOREIGN KEY (session_id, session_turn_id) REFERENCES session_turns (session_id, id),
    ADD FOREIGN KEY (session_id, published_seq) REFERENCES session_events (session_id, seq);
CREATE INDEX action_proposals_session_idx
    ON action_proposals (session_id, session_turn_id, id) WHERE session_id IS NOT NULL;

ALTER TABLE approvals ADD COLUMN revoked_at timestamptz;

CREATE SEQUENCE action_lock_generation;
ALTER TABLE action_locks
    ADD COLUMN owner text,
    ADD COLUMN generation bigint,
    ADD COLUMN expires_at timestamptz,
    ADD COLUMN session_owner text,
    ADD COLUMN session_generation bigint,
    ADD CHECK (
        (owner IS NULL AND generation IS NULL AND expires_at IS NULL
            AND session_owner IS NULL AND session_generation IS NULL)
        OR (owner IS NOT NULL AND generation IS NOT NULL AND expires_at IS NOT NULL
            AND session_owner IS NOT NULL AND session_generation IS NOT NULL)
    );

ALTER TABLE action_attempts
    ADD COLUMN send_granted_at timestamptz,
    ADD COLUMN request_timeout_seconds integer CHECK (request_timeout_seconds > 0),
    ADD COLUMN reconciliation jsonb;
