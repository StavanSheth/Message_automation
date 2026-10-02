-- 008_phase4_final_integrity.sql
-- Phase 4 Hardening: Browser session ownership persistence, database indexes, and integrity constraints

-- 1. Browser sessions account tracking
ALTER TABLE browser_sessions ADD COLUMN account_id TEXT;
CREATE INDEX IF NOT EXISTS idx_browser_sessions_account_id ON browser_sessions(account_id);
CREATE INDEX IF NOT EXISTS idx_browser_sessions_worker_id ON browser_sessions(worker_id);

-- 2. Enhanced indexing for operational queries and integrity
CREATE INDEX IF NOT EXISTS idx_tasks_account_priority ON tasks(account_id, priority DESC, scheduled_at ASC);
CREATE INDEX IF NOT EXISTS idx_accounts_active_status ON accounts(status, daily_sends_count);
CREATE INDEX IF NOT EXISTS idx_rate_limit_cooldowns_expiry ON rate_limit_cooldowns(cooldown_until, is_active);
CREATE INDEX IF NOT EXISTS idx_diagnostic_artifacts_timestamp ON diagnostic_artifacts(timestamp);
