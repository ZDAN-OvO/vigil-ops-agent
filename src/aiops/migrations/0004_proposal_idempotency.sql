-- 同一目标版本只允许一个进行中的提案；失败或拒绝后可以重新提案。
ALTER TABLE action_proposals DROP CONSTRAINT action_proposals_idempotency_key_key;

CREATE UNIQUE INDEX action_proposals_active_key
    ON action_proposals (idempotency_key)
    WHERE status IN ('pending_approval', 'approved', 'executing', 'unknown');
