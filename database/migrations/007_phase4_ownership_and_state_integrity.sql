-- 007_phase4_ownership_and_state_integrity.sql
-- Phase 4 Remediation: Ownership integrity, manual review resolution metadata, and operational indexing

-- 1. Account worker and session exclusivity
CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_active_worker ON accounts(assigned_worker_id) WHERE assigned_worker_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_active_session ON accounts(assigned_session_id) WHERE assigned_session_id IS NOT NULL;

-- 2. Worker account indexing
CREATE INDEX IF NOT EXISTS idx_workers_account_id ON workers(account_id);

-- 3. Task priority and account composite index for scheduler queries
CREATE INDEX IF NOT EXISTS idx_tasks_account_status_priority ON tasks(account_id, status, priority DESC);

-- 4. Manual review resolution tracking columns
ALTER TABLE manual_reviews ADD COLUMN resolution TEXT;
ALTER TABLE manual_reviews ADD COLUMN resolved_by TEXT;
ALTER TABLE manual_reviews ADD COLUMN resolution_notes TEXT;

CREATE INDEX IF NOT EXISTS idx_manual_reviews_resolved_at ON manual_reviews(resolved_at);
CREATE INDEX IF NOT EXISTS idx_reconciliations_updated_at ON reconciliations(updated_at);
CREATE INDEX IF NOT EXISTS idx_rate_limit_cooldowns_scope_account ON rate_limit_cooldowns(scope, account_id, is_active);
