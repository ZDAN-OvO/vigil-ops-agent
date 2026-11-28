-- 新旧入口共用故障行和发送名额。按名称查询也要能找到替换前的工作负载。
CREATE INDEX remediation_fault_name_history
    ON remediation_faults (cluster, namespace, workload_kind, workload_name, cleared_at DESC);

CREATE TABLE remediation_inspection_signals (
    fault_id text NOT NULL REFERENCES remediation_faults (id),
    run_id text NOT NULL REFERENCES inspection_runs (id),
    required_checks jsonb NOT NULL CHECK (jsonb_typeof(required_checks) = 'array'),
    observation_ids text[] NOT NULL,
    abnormal_at timestamptz,
    healthy_at timestamptz,
    PRIMARY KEY (fault_id, run_id),
    CHECK (num_nonnulls(abnormal_at, healthy_at) <= 1)
);

CREATE TABLE remediation_inspection_reviews (
    id text PRIMARY KEY,
    run_id text NOT NULL REFERENCES inspection_runs (id),
    fault_id text NOT NULL REFERENCES remediation_faults (id),
    turn_id text NOT NULL REFERENCES session_turns (id),
    investigation_id text NOT NULL REFERENCES investigations (id),
    rule_id text NOT NULL,
    policy_version text NOT NULL,
    sop_hash text NOT NULL,
    observation_ids text[] NOT NULL CHECK (cardinality(observation_ids) > 0),
    conclusion text NOT NULL,
    trigger_check text NOT NULL,
    max_evidence_age_seconds integer NOT NULL CHECK (max_evidence_age_seconds > 0),
    UNIQUE (run_id, fault_id)
);

ALTER TABLE remediation_jobs ADD COLUMN inspection_review_id text
    REFERENCES remediation_inspection_reviews (id);
