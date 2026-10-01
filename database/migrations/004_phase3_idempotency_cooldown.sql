-- 004_phase3_idempotency_cooldown.sql
-- Production execution idempotency, persistent rate-limit cooldown, and integrity constraints

-- 1. Durable execution identities table for cross-worker / cross-process idempotency
CREATE TABLE IF NOT EXISTS execution_identities (
    execution_key TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    message_id TEXT,
    contact_id TEXT NOT NULL,
    message_hash TEXT,
    attempt INTEGER NOT NULL DEFAULT 1,
    worker_id TEXT,
    session_id TEXT,
    correlation_id TEXT,
    state TEXT NOT NULL CHECK (state IN ('CREATED', 'RUNNING', 'SENT', 'RECONCILIATION', 'FAILED', 'MANUAL_REVIEW')),
    outcome TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_exec_task ON execution_identities(task_id);
CREATE INDEX IF NOT EXISTS idx_exec_contact ON execution_identities(contact_id);

-- 2. Persistent rate-limit cooldown table
CREATE TABLE IF NOT EXISTS rate_limit_cooldowns (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN ('GLOBAL', 'ACCOUNT', 'WORKER', 'SESSION')),
    account_id TEXT,
    reason TEXT NOT NULL,
    error_code TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    cooldown_until TEXT NOT NULL,
    detected_by_worker TEXT,
    detected_by_session TEXT,
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_cooldown_active ON rate_limit_cooldowns(scope, is_active, cooldown_until);

-- 3. Uniqueness on active reconciliations to prevent duplicate unresolved reconciliation records
CREATE UNIQUE INDEX IF NOT EXISTS idx_reconciliations_active_task ON reconciliations(task_id) WHERE state IN ('PENDING', 'IN_PROGRESS');

-- 4. Observability indexing on events
CREATE INDEX IF NOT EXISTS idx_events_worker ON events(worker_id);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_code_time ON events(event_code, timestamp);
