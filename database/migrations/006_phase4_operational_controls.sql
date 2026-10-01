-- 006_phase4_operational_controls.sql
-- Phase 4 Operational Control Plane: Accounts, session tracking, task priorities, and operational metrics

CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'PAUSED', 'CHALLENGED', 'SUSPENDED', 'BANNED')),
    profile_path TEXT,
    assigned_worker_id TEXT,
    assigned_session_id TEXT,
    daily_send_limit INTEGER NOT NULL DEFAULT 50,
    daily_sends_count INTEGER NOT NULL DEFAULT 0,
    last_send_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_accounts_username ON accounts(username);
CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status);
CREATE INDEX IF NOT EXISTS idx_accounts_worker ON accounts(assigned_worker_id);

-- Add account_id to tasks and workers
ALTER TABLE tasks ADD COLUMN account_id TEXT;
CREATE INDEX IF NOT EXISTS idx_tasks_account ON tasks(account_id);
CREATE INDEX IF NOT EXISTS idx_tasks_priority_scheduled ON tasks(priority DESC, scheduled_at ASC, created_at ASC);

ALTER TABLE workers ADD COLUMN account_id TEXT;
ALTER TABLE workers ADD COLUMN quarantine_reason TEXT;

-- System controls key-value persistence
CREATE TABLE IF NOT EXISTS system_controls (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
