-- 003_phase3_leases_reconciliations.sql
-- Production task leases, message deduplication, reconciliations, manual reviews, and observability indexing

-- 1. Task lease and idempotency fields
ALTER TABLE tasks ADD COLUMN lease_id TEXT;
ALTER TABLE tasks ADD COLUMN lease_owner TEXT;
ALTER TABLE tasks ADD COLUMN lease_acquired_at TEXT;
ALTER TABLE tasks ADD COLUMN lease_expires_at TEXT;
ALTER TABLE tasks ADD COLUMN message_hash TEXT;

CREATE INDEX IF NOT EXISTS idx_tasks_lease ON tasks(lease_id, lease_owner, lease_expires_at);

-- 2. Message duplicate protection: at most one SENT message per task
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_task_sent ON messages(task_id) WHERE status = 'SENT';

-- 3. Reconciliations table
CREATE TABLE IF NOT EXISTS reconciliations (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    message_id TEXT,
    worker_id TEXT,
    session_id TEXT,
    state TEXT NOT NULL CHECK (state IN ('PENDING', 'IN_PROGRESS', 'RESOLVED', 'FAILED')),
    reason TEXT NOT NULL,
    observed_state TEXT,
    resolution TEXT CHECK (resolution IN ('CONFIRMED_SENT', 'CONFIRMED_NOT_SENT', 'MANUAL_REVIEW', 'RETRY_ALLOWED')),
    resolution_source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_reconciliations_task ON reconciliations(task_id);
CREATE INDEX IF NOT EXISTS idx_reconciliations_state ON reconciliations(state);

-- 4. Manual Review Queue table
CREATE TABLE IF NOT EXISTS manual_reviews (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    contact_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    current_state TEXT NOT NULL,
    evidence_json TEXT,
    recommended_action TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'RESOLVED', 'DISMISSED')),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_manual_reviews_status ON manual_reviews(status);
CREATE INDEX IF NOT EXISTS idx_manual_reviews_task ON manual_reviews(task_id);

-- 5. Observability fields on events table
ALTER TABLE events ADD COLUMN task_id TEXT;
ALTER TABLE events ADD COLUMN worker_id TEXT;
ALTER TABLE events ADD COLUMN session_id TEXT;
ALTER TABLE events ADD COLUMN correlation_id TEXT;

CREATE INDEX IF NOT EXISTS idx_events_correlation ON events(correlation_id);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id);
