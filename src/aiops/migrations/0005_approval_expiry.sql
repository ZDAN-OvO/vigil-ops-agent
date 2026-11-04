-- 审批过期：授权在有效期内才可继续执行，过期作为独立终态留档。

ALTER TABLE action_proposals DROP CONSTRAINT action_proposals_status_check;
ALTER TABLE action_proposals ADD CONSTRAINT action_proposals_status_check CHECK (
    status IN (
        'pending_approval', 'approved', 'rejected', 'denied',
        'executing', 'succeeded', 'failed', 'unknown', 'expired'
    )
);

CREATE INDEX IF NOT EXISTS approvals_proposal_idx ON approvals (proposal_id, decided_at DESC);
