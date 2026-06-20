-- RobinHealth: background job queue schema
-- Standalone -- no dependency on schema.sql or bills_schema.sql, so this
-- can be applied in any order relative to them.
-- Postgres 14+

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TYPE job_status AS ENUM ('pending', 'in_progress', 'completed', 'failed');

CREATE TABLE jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status job_status NOT NULL DEFAULT 'pending',
    attempts INT NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

-- Partial index matching exactly the claim query's predicate (status =
-- 'pending', ordered by created_at) -- a full index on status alone
-- would also cover completed/failed/in_progress rows the claim query
-- never looks at.
CREATE INDEX idx_jobs_pending_created ON jobs(created_at) WHERE status = 'pending';
