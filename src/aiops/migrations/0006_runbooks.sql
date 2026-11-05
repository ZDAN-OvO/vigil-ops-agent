-- Runbook 知识：调查只能查询与引用，不能凭它授权动作。

CREATE TABLE IF NOT EXISTS runbooks (
    id text PRIMARY KEY,
    version integer NOT NULL,
    title text NOT NULL,
    summary text NOT NULL,
    applies_to text[] NOT NULL DEFAULT '{}',
    symptoms text[] NOT NULL DEFAULT '{}',
    steps jsonb NOT NULL,
    source text NOT NULL,
    updated_at timestamptz NOT NULL
);
