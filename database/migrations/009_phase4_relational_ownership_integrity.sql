-- 009_phase4_relational_ownership_integrity.sql
-- Phase 4 Hardening: Relational ownership enforcement across Task -> Account -> Worker -> BrowserSession -> Profile

-- 1. Ensure Task account_id references existing accounts table (when non-null)
CREATE TRIGGER IF NOT EXISTS trg_tasks_account_fk_insert
BEFORE INSERT ON tasks
FOR EACH ROW
WHEN NEW.account_id IS NOT NULL
BEGIN
    SELECT CASE
        WHEN (SELECT 1 FROM accounts WHERE id = NEW.account_id) IS NULL THEN
            RAISE(ABORT, 'Foreign key violation: tasks.account_id does not exist in accounts')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_tasks_account_fk_update
BEFORE UPDATE OF account_id ON tasks
FOR EACH ROW
WHEN NEW.account_id IS NOT NULL
BEGIN
    SELECT CASE
        WHEN (SELECT 1 FROM accounts WHERE id = NEW.account_id) IS NULL THEN
            RAISE(ABORT, 'Foreign key violation: tasks.account_id does not exist in accounts')
    END;
END;

-- 2. Ensure Worker account_id references existing accounts table (when non-null)
CREATE TRIGGER IF NOT EXISTS trg_workers_account_fk_insert
BEFORE INSERT ON workers
FOR EACH ROW
WHEN NEW.account_id IS NOT NULL
BEGIN
    SELECT CASE
        WHEN (SELECT 1 FROM accounts WHERE id = NEW.account_id) IS NULL THEN
            RAISE(ABORT, 'Foreign key violation: workers.account_id does not exist in accounts')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_workers_account_fk_update
BEFORE UPDATE OF account_id ON workers
FOR EACH ROW
WHEN NEW.account_id IS NOT NULL
BEGIN
    SELECT CASE
        WHEN (SELECT 1 FROM accounts WHERE id = NEW.account_id) IS NULL THEN
            RAISE(ABORT, 'Foreign key violation: workers.account_id does not exist in accounts')
    END;
END;

-- 3. Ensure BrowserSession account_id references existing accounts table (when non-null)
CREATE TRIGGER IF NOT EXISTS trg_browser_sessions_account_fk_insert
BEFORE INSERT ON browser_sessions
FOR EACH ROW
WHEN NEW.account_id IS NOT NULL
BEGIN
    SELECT CASE
        WHEN (SELECT 1 FROM accounts WHERE id = NEW.account_id) IS NULL THEN
            RAISE(ABORT, 'Foreign key violation: browser_sessions.account_id does not exist in accounts')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_browser_sessions_account_fk_update
BEFORE UPDATE OF account_id ON browser_sessions
FOR EACH ROW
WHEN NEW.account_id IS NOT NULL
BEGIN
    SELECT CASE
        WHEN (SELECT 1 FROM accounts WHERE id = NEW.account_id) IS NULL THEN
            RAISE(ABORT, 'Foreign key violation: browser_sessions.account_id does not exist in accounts')
    END;
END;

-- 4. Enforce task.account_id == worker.account_id when task is assigned to worker
CREATE TRIGGER IF NOT EXISTS trg_tasks_worker_account_match_insert
BEFORE INSERT ON tasks
FOR EACH ROW
WHEN NEW.account_id IS NOT NULL AND NEW.worker_id IS NOT NULL
BEGIN
    SELECT CASE
        WHEN (SELECT account_id FROM workers WHERE id = NEW.worker_id) IS NOT NULL
         AND (SELECT account_id FROM workers WHERE id = NEW.worker_id) != NEW.account_id THEN
            RAISE(ABORT, 'Ownership mismatch: task.account_id does not match worker.account_id')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_tasks_worker_account_match_update
BEFORE UPDATE OF worker_id, account_id ON tasks
FOR EACH ROW
WHEN NEW.account_id IS NOT NULL AND NEW.worker_id IS NOT NULL
BEGIN
    SELECT CASE
        WHEN (SELECT account_id FROM workers WHERE id = NEW.worker_id) IS NOT NULL
         AND (SELECT account_id FROM workers WHERE id = NEW.worker_id) != NEW.account_id THEN
            RAISE(ABORT, 'Ownership mismatch: task.account_id does not match worker.account_id')
    END;
END;

-- 5. Enforce browser_sessions.account_id == worker.account_id when session is assigned to worker
CREATE TRIGGER IF NOT EXISTS trg_sessions_worker_account_match_insert
BEFORE INSERT ON browser_sessions
FOR EACH ROW
WHEN NEW.account_id IS NOT NULL AND NEW.worker_id IS NOT NULL
BEGIN
    SELECT CASE
        WHEN (SELECT account_id FROM workers WHERE id = NEW.worker_id) IS NOT NULL
         AND (SELECT account_id FROM workers WHERE id = NEW.worker_id) != NEW.account_id THEN
            RAISE(ABORT, 'Ownership mismatch: browser_sessions.account_id does not match worker.account_id')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_sessions_worker_account_match_update
BEFORE UPDATE OF worker_id, account_id ON browser_sessions
FOR EACH ROW
WHEN NEW.account_id IS NOT NULL AND NEW.worker_id IS NOT NULL
BEGIN
    SELECT CASE
        WHEN (SELECT account_id FROM workers WHERE id = NEW.worker_id) IS NOT NULL
         AND (SELECT account_id FROM workers WHERE id = NEW.worker_id) != NEW.account_id THEN
            RAISE(ABORT, 'Ownership mismatch: browser_sessions.account_id does not match worker.account_id')
    END;
END;

-- 6. Enforce account.assigned_worker_id and account.assigned_session_id belongs to the account
CREATE TRIGGER IF NOT EXISTS trg_accounts_assigned_worker_match
BEFORE UPDATE OF assigned_worker_id ON accounts
FOR EACH ROW
WHEN NEW.assigned_worker_id IS NOT NULL
BEGIN
    SELECT CASE
        WHEN (SELECT account_id FROM workers WHERE id = NEW.assigned_worker_id) IS NOT NULL
         AND (SELECT account_id FROM workers WHERE id = NEW.assigned_worker_id) != NEW.id THEN
            RAISE(ABORT, 'Ownership mismatch: assigned_worker_id does not belong to this account')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_accounts_assigned_session_match
BEFORE UPDATE OF assigned_session_id ON accounts
FOR EACH ROW
WHEN NEW.assigned_session_id IS NOT NULL
BEGIN
    SELECT CASE
        WHEN (SELECT account_id FROM browser_sessions WHERE id = NEW.assigned_session_id) IS NOT NULL
         AND (SELECT account_id FROM browser_sessions WHERE id = NEW.assigned_session_id) != NEW.id THEN
            RAISE(ABORT, 'Ownership mismatch: assigned_session_id does not belong to this account')
    END;
END;

-- 7. Enforce browser_sessions profile_path matches account profile_path when assigned
CREATE TRIGGER IF NOT EXISTS trg_sessions_profile_match_insert
BEFORE INSERT ON browser_sessions
FOR EACH ROW
WHEN NEW.account_id IS NOT NULL AND NEW.profile_path IS NOT NULL
BEGIN
    SELECT CASE
        WHEN (SELECT profile_path FROM accounts WHERE id = NEW.account_id) IS NOT NULL
         AND (SELECT profile_path FROM accounts WHERE id = NEW.account_id) != NEW.profile_path THEN
            RAISE(ABORT, 'Ownership mismatch: browser_session profile does not match account profile')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_sessions_profile_match_update
BEFORE UPDATE OF profile_path, account_id ON browser_sessions
FOR EACH ROW
WHEN NEW.account_id IS NOT NULL AND NEW.profile_path IS NOT NULL
BEGIN
    SELECT CASE
        WHEN (SELECT profile_path FROM accounts WHERE id = NEW.account_id) IS NOT NULL
         AND (SELECT profile_path FROM accounts WHERE id = NEW.account_id) != NEW.profile_path THEN
            RAISE(ABORT, 'Ownership mismatch: browser_session profile does not match account profile')
    END;
END;
