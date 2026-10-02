-- 010_phase5_hardening.sql
-- Phase 5 Hardening: Observability completeness, indexing, and DB-level immutability triggers

-- 1. Add account_id to events table for complete observability attribution
ALTER TABLE events ADD COLUMN account_id TEXT;
CREATE INDEX IF NOT EXISTS idx_events_account ON events(account_id);

-- 2. Index for lease expiry and status scans during startup and scheduler recovery
CREATE INDEX IF NOT EXISTS idx_tasks_status_lease_exp ON tasks(status, lease_expires_at);

-- 3. Prevent COMPLETED tasks from being reverted back to active execution states
CREATE TRIGGER IF NOT EXISTS trg_tasks_prevent_sent_reversal
BEFORE UPDATE OF status ON tasks
FOR EACH ROW
WHEN OLD.status = 'COMPLETED' AND NEW.status IN ('READY', 'QUEUED', 'RUNNING', 'SENDING', 'VERIFYING')
BEGIN
    SELECT RAISE(ABORT, 'Illegal task state transition: COMPLETED task cannot be transitioned back to active execution');
END;

-- 4. Prevent confirmed SENT execution identities from being reopened
CREATE TRIGGER IF NOT EXISTS trg_execution_identities_prevent_sent_reversal
BEFORE UPDATE OF state ON execution_identities
FOR EACH ROW
WHEN OLD.state = 'SENT' AND NEW.state IN ('RUNNING', 'PENDING')
BEGIN
    SELECT RAISE(ABORT, 'Illegal execution identity transition: SENT identity cannot be reopened');
END;
