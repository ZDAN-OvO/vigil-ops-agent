-- 结构化告警、故障名额和后台处置都须在重启后保留。
ALTER TABLE action_proposals DROP CONSTRAINT action_proposals_kind_check;
ALTER TABLE action_proposals ADD CONSTRAINT action_proposals_kind_check CHECK (
    kind IN ('restart_pod', 'rollout_undo', 'scale_replicas', 'adjust_resources', 'update_config')
);
CREATE INDEX action_locks_proposal_idx ON action_locks (proposal_id, target_key);

-- 切换前的未发送卡片立即作废，历史消息和已经取得发送权的记录可继续核对。
UPDATE approvals SET revoked_at = clock_timestamp()
WHERE revoked_at IS NULL AND decision = 'approved' AND proposal_id IN (
    SELECT p.id FROM action_proposals p
    LEFT JOIN action_attempts a ON a.proposal_id = p.id
    WHERE p.kind IN ('restart_pod', 'rollout_undo') AND
        p.status IN ('pending_approval', 'approved')
        AND (a.id IS NULL OR (a.status = 'intent' AND a.send_granted_at IS NULL))
);
UPDATE action_proposals p SET status = 'voided'
WHERE p.kind IN ('restart_pod', 'rollout_undo')
  AND p.status IN ('pending_approval', 'approved') AND NOT EXISTS (
    SELECT 1 FROM action_attempts a WHERE a.proposal_id = p.id
      AND (a.status <> 'intent' OR a.send_granted_at IS NOT NULL)
);
UPDATE action_attempts a SET status = 'voided', finished_at = clock_timestamp(),
    result = '{"code":"legacy_action_disabled"}'
WHERE a.status = 'intent' AND a.send_granted_at IS NULL AND EXISTS (
    SELECT 1 FROM action_proposals p WHERE p.id = a.proposal_id
      AND p.kind IN ('restart_pod', 'rollout_undo') AND p.status = 'voided'
);
DELETE FROM action_locks l USING action_proposals p
WHERE l.proposal_id = p.id AND p.kind IN ('restart_pod', 'rollout_undo')
  AND p.status = 'voided'
  AND NOT EXISTS (
      SELECT 1 FROM action_attempts a WHERE a.proposal_id = p.id AND
        (a.status <> 'voided' OR a.send_granted_at IS NOT NULL)
  );

CREATE TABLE remediation_faults (
    id text PRIMARY KEY,
    cluster text NOT NULL,
    namespace text NOT NULL,
    workload_kind text NOT NULL,
    workload_name text NOT NULL,
    workload_uid text NOT NULL,
    opened_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    cleared_at timestamptz,
    CHECK (cleared_at IS NULL OR cleared_at >= opened_at)
);

CREATE UNIQUE INDEX remediation_open_fault
    ON remediation_faults (cluster, namespace, workload_uid) WHERE cleared_at IS NULL;
CREATE INDEX remediation_fault_history
    ON remediation_faults (cluster, namespace, workload_uid, cleared_at DESC);

CREATE TABLE remediation_alerts (
    cluster text NOT NULL,
    group_key text NOT NULL,
    fingerprint text NOT NULL,
    starts_at timestamptz NOT NULL,
    labels jsonb NOT NULL CHECK (jsonb_typeof(labels) = 'object'),
    session_id text REFERENCES sessions (id),
    fault_id text REFERENCES remediation_faults (id),
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    resolved_at timestamptz,
    evaluated_at timestamptz,
    evaluation_owner text,
    evaluation_expires_at timestamptz,
    evaluate_after timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (cluster, group_key, fingerprint, starts_at),
    CHECK ((evaluation_owner IS NULL) = (evaluation_expires_at IS NULL))
);

CREATE INDEX remediation_alerts_ready ON remediation_alerts (evaluate_after)
    WHERE evaluated_at IS NULL AND session_id IS NOT NULL;
CREATE INDEX remediation_alerts_group ON remediation_alerts (cluster, group_key, session_id)
    WHERE resolved_at IS NULL;
CREATE INDEX remediation_alerts_fault ON remediation_alerts (fault_id)
    WHERE fault_id IS NOT NULL;

CREATE TABLE remediation_jobs (
    proposal_id text PRIMARY KEY REFERENCES action_proposals (id),
    fault_id text REFERENCES remediation_faults (id),
    parent_proposal_id text UNIQUE REFERENCES remediation_jobs (proposal_id),
    automated boolean NOT NULL,
    resource_keys text[] NOT NULL CHECK (cardinality(resource_keys) BETWEEN 1 AND 2),
    state text NOT NULL CHECK (
        state IN ('awaiting_approval', 'ready', 'sending', 'applying', 'observing', 'finished')
    ),
    run_after timestamptz NOT NULL DEFAULT clock_timestamp(),
    owner text,
    generation bigint,
    expires_at timestamptz,
    sent_at timestamptz,
    applied_at timestamptz,
    effective_at timestamptz,
    -- 所有足以影响放行或补偿的证据都存于账本，便于换代接手。
    evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence) = 'object'),
    observation jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(observation) = 'object'),
    CHECK (num_nonnulls(owner, generation, expires_at) IN (0, 3)),
    CHECK (NOT automated OR fault_id IS NOT NULL),
    CHECK (parent_proposal_id IS NULL OR automated),
    CHECK (parent_proposal_id IS DISTINCT FROM proposal_id),
    CHECK (applied_at IS NULL OR sent_at IS NOT NULL),
    CHECK (effective_at IS NULL OR applied_at IS NOT NULL)
);
CREATE UNIQUE INDEX remediation_one_automatic_send
    ON remediation_jobs (fault_id, (parent_proposal_id IS NOT NULL))
    WHERE automated AND sent_at IS NOT NULL;
CREATE INDEX remediation_jobs_ready ON remediation_jobs (run_after, proposal_id)
    WHERE state IN ('ready', 'sending', 'applying', 'observing');
CREATE INDEX remediation_jobs_fault ON remediation_jobs (fault_id)
    WHERE fault_id IS NOT NULL;
