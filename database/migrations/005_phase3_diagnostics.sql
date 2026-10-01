-- 005_phase3_diagnostics.sql
-- Production diagnostic artifacts table and indexing for failure analysis and cleanup retention

CREATE TABLE IF NOT EXISTS diagnostic_artifacts (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    worker_id TEXT,
    session_id TEXT,
    correlation_id TEXT,
    timestamp TEXT NOT NULL,
    artifact_type TEXT NOT NULL CHECK (artifact_type IN ('SCREENSHOT', 'HTML', 'PAGE_STATE', 'ERROR_METADATA', 'BROWSER_LOG')),
    file_path TEXT,
    page_url TEXT,
    page_title TEXT,
    error_code TEXT,
    reason TEXT,
    retention_until TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_diag_retention ON diagnostic_artifacts(retention_until);
CREATE INDEX IF NOT EXISTS idx_diag_task ON diagnostic_artifacts(task_id);
CREATE INDEX IF NOT EXISTS idx_diag_correlation ON diagnostic_artifacts(correlation_id);
