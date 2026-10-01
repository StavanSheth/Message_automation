-- 001_initial_schema.sql
-- Initial database schema for Instagram Browser Automation System (Phase 1)

PRAGMA foreign_keys = ON;

-- Migration tracker
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- Contacts table
CREATE TABLE IF NOT EXISTS contacts (
    id TEXT PRIMARY KEY,
    source_record_id TEXT,
    name TEXT NOT NULL,
    instagram_url TEXT NOT NULL,
    username TEXT,
    expected_followers INTEGER,
    notes TEXT,
    replied_status TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK (replied_status IN ('UNKNOWN', 'YES', 'NO')),
    replied_source TEXT NOT NULL DEFAULT 'MANUAL',
    replied_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_contacts_instagram_url ON contacts(instagram_url);
CREATE INDEX IF NOT EXISTS idx_contacts_replied_status ON contacts(replied_status);

-- Source records (audit / mirror of incoming spreadsheet rows)
CREATE TABLE IF NOT EXISTS source_records (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL CHECK (source_type IN ('LOCAL_XLSX', 'BROWSER_SPREADSHEET')),
    source_identifier TEXT NOT NULL,
    row_index INTEGER NOT NULL,
    raw_data_json TEXT NOT NULL,
    checksum TEXT NOT NULL,
    last_synced_at TEXT NOT NULL,
    contact_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_source_records_ident_row ON source_records(source_identifier, row_index);

-- Tasks table
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    contact_id TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('MESSAGE', 'FOLLOW_UP_1', 'FOLLOW_UP_2')),
    sequence INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN (
        'CREATED', 'VALIDATING', 'QUEUED', 'READY', 'RUNNING',
        'SENDING', 'VERIFYING',
        'COMPLETED', 'RETRY_WAIT', 'MANUAL_REVIEW', 'SKIPPED',
        'CANCELLED', 'RECONCILING', 'INTERRUPTED', 'FAILED'
    )),
    priority INTEGER NOT NULL DEFAULT 0,
    scheduled_at TEXT,
    started_at TEXT,
    completed_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    worker_id TEXT,
    last_error_id TEXT,
    lock_token TEXT,
    locked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_contact_type_seq ON tasks(contact_id, type, sequence);
CREATE INDEX IF NOT EXISTS idx_tasks_status_scheduled ON tasks(status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_tasks_worker ON tasks(worker_id);

-- Messages table
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    contact_id TEXT NOT NULL,
    task_id TEXT,
    sequence INTEGER NOT NULL DEFAULT 0,
    body TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'PENDING', 'VERIFYING', 'VERIFIED', 'AWAITING_APPROVAL',
        'APPROVED', 'SENDING', 'SENT', 'FAILED', 'SKIPPED', 'RECONCILIATION'
    )),
    attempted_at TEXT,
    confirmed_at TEXT,
    result_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_contact ON messages(contact_id);
CREATE INDEX IF NOT EXISTS idx_messages_task ON messages(task_id);

-- Followups table
CREATE TABLE IF NOT EXISTS followups (
    id TEXT PRIMARY KEY,
    contact_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence IN (1, 2)),
    message TEXT NOT NULL,
    delay_seconds INTEGER NOT NULL,
    scheduled_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'PENDING', 'SCHEDULED', 'DUE', 'SENT', 'CANCELLED', 'SKIPPED'
    )),
    sent_at TEXT,
    cancelled_at TEXT,
    cancel_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_followups_contact_seq ON followups(contact_id, sequence);
CREATE INDEX IF NOT EXISTS idx_followups_scheduled ON followups(status, scheduled_at);

-- Verification results
CREATE TABLE IF NOT EXISTS verification_results (
    id TEXT PRIMARY KEY,
    contact_id TEXT NOT NULL,
    task_id TEXT,
    confidence REAL NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN (
        'HIGH_CONFIDENCE', 'MEDIUM_CONFIDENCE', 'LOW_CONFIDENCE', 'MISMATCH', 'NOT_FOUND'
    )),
    signals_json TEXT NOT NULL,
    ocr_text TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_verification_task ON verification_results(task_id);

-- Automation runs
CREATE TABLE IF NOT EXISTS automation_runs (
    id TEXT PRIMARY KEY,
    run_code TEXT NOT NULL UNIQUE,
    worker_id TEXT,
    task_id TEXT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    metadata_json TEXT
);

-- Workers table
CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    worker_code TEXT NOT NULL UNIQUE,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    current_task_id TEXT,
    last_heartbeat TEXT,
    metadata_json TEXT
);

-- Browser sessions
CREATE TABLE IF NOT EXISTS browser_sessions (
    id TEXT PRIMARY KEY,
    worker_id TEXT,
    profile_path TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    closed_at TEXT
);

-- Events table (append-oriented)
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    level TEXT NOT NULL CHECK (level IN ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')),
    category TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    event_code TEXT NOT NULL,
    payload_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_events_code ON events(event_code);

-- Errors table (structured error tracking)
CREATE TABLE IF NOT EXISTS errors (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
    retryable INTEGER NOT NULL CHECK (retryable IN (0, 1)),
    attempt INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_errors_task ON errors(task_id);
CREATE INDEX IF NOT EXISTS idx_errors_code ON errors(code);

-- Sync runs table
CREATE TABLE IF NOT EXISTS sync_runs (
    id TEXT PRIMARY KEY,
    sync_code TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL,
    source_identifier TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'SUCCESS', 'PARTIAL', 'FAILED', 'CONFLICT')),
    records_read INTEGER NOT NULL DEFAULT 0,
    records_written INTEGER NOT NULL DEFAULT 0,
    conflicts INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_sync_runs_ident ON sync_runs(source_identifier);

-- Settings table
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
