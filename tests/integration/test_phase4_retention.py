"""Integration tests for operational data retention and safe terminal record pruning (Section 9)."""

import pytest
from datetime import datetime, timezone, timedelta
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.event_repo import EventRepository
from backend.repositories.error_repo import ErrorRepository
from backend.repositories.reconciliation_repo import ReconciliationRepository
from backend.repositories.manual_review_repo import ManualReviewRepository
from backend.repositories.execution_identity_repo import ExecutionIdentityRepository
from backend.repositories.task_repo import TaskRepository
from backend.repositories.contact_repo import ContactRepository
from backend.application.retention_service import RetentionService
from backend.domain.models import ErrorRecord, ReconciliationRecord, ManualReviewItem, ExecutionIdentity, Task, Contact
from backend.domain.enums import ErrorCode, ErrorSeverity, TaskState, TaskType, EventCode, EventLevel


@pytest.fixture
def retention_env(tmp_path):
    db_path = str(tmp_path / "test_retention_integ.db")
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()

    event_repo = EventRepository(db)
    error_repo = ErrorRepository(db)
    rec_repo = ReconciliationRepository(db)
    review_repo = ManualReviewRepository(db)
    exec_id_repo = ExecutionIdentityRepository(db)
    task_repo = TaskRepository(db)
    contact_repo = ContactRepository(db)

    ret_svc = RetentionService(db=db, event_repo=event_repo)

    return {
        "db": db,
        "event_repo": event_repo,
        "error_repo": error_repo,
        "rec_repo": rec_repo,
        "review_repo": review_repo,
        "exec_id_repo": exec_id_repo,
        "task_repo": task_repo,
        "contact_repo": contact_repo,
        "ret_svc": ret_svc,
    }


def test_retention_cleans_only_safe_terminal_records(retention_env):
    db = retention_env["db"]
    event_repo = retention_env["event_repo"]
    error_repo = retention_env["error_repo"]
    rec_repo = retention_env["rec_repo"]
    review_repo = retention_env["review_repo"]
    task_repo = retention_env["task_repo"]
    contact_repo = retention_env["contact_repo"]
    ret_svc = retention_env["ret_svc"]

    contact_repo.create(Contact(id="cnt-ret-1", name="Ret Contact", instagram_url="https://instagram.com/ret"))
    task = Task(id="t-ret-active", contact_id="cnt-ret-1", type=TaskType.MESSAGE, status=TaskState.SENDING)
    task_repo.create(task)

    now = datetime.now(timezone.utc)
    old_time = (now - timedelta(days=40)).isoformat()
    recent_time = (now - timedelta(days=2)).isoformat()

    # 1. Events: one old, one recent
    event_repo.record(EventCode.TASK_CLAIMED, category="task", level=EventLevel.INFO, payload={"test": "old"})
    with db.transaction() as conn:
        conn.execute("UPDATE events SET timestamp = ?;", (old_time,))
    event_repo.record(EventCode.TASK_CLAIMED, category="task", level=EventLevel.INFO, payload={"test": "recent"})

    # 2. Errors: one old resolved, one old UNRESOLVED
    error_repo.record(
        ErrorRecord(
            id="err-old-resolved",
            task_id="t-ret-active",
            code=ErrorCode.TIMEOUT,
            message="Old timeout resolved",
            severity=ErrorSeverity.LOW,
            retryable=False,
            created_at=old_time,
            resolved_at=old_time,
        )
    )
    error_repo.record(
        ErrorRecord(
            id="err-old-unresolved",
            task_id="t-ret-active",
            code=ErrorCode.TIMEOUT,
            message="Old timeout UNRESOLVED",
            severity=ErrorSeverity.HIGH,
            retryable=True,
            created_at=old_time,
            resolved_at=None,
        )
    )

    # 3. Reconciliations: one RESOLVED (old), one PENDING (old)
    rec_repo.create(
        ReconciliationRecord(
            id="rec-resolved-old",
            task_id="t-ret-active",
            state="RESOLVED",
            created_at=old_time,
            updated_at=old_time,
            resolved_at=old_time,
        )
    )
    rec_repo.create(
        ReconciliationRecord(
            id="rec-pending-old",
            task_id="t-ret-active",
            state="PENDING",
            created_at=old_time,
            updated_at=old_time,
        )
    )

    # 4. Manual Reviews: one RESOLVED (old), one PENDING (old)
    review_repo.create(
        ManualReviewItem(
            id="rev-resolved-old",
            task_id="t-ret-active",
            contact_id="cnt-ret-1",
            reason="Ambiguous send",
            current_state="RECONCILING",
            status="RESOLVED",
            created_at=old_time,
            resolved_at=old_time,
        )
    )
    review_repo.create(
        ManualReviewItem(
            id="rev-pending-old",
            task_id="t-ret-active",
            contact_id="cnt-ret-1",
            reason="Ambiguous send",
            current_state="RECONCILING",
            status="PENDING",
            created_at=old_time,
        )
    )

    # Execute retention cleanup with 30-day window
    summary = ret_svc.cleanup_expired_data(event_retention_days=30, error_retention_days=30, resolved_retention_days=30)

    # Verify pruned counts
    assert summary["events_deleted"] == 1
    assert summary["errors_deleted"] == 1
    assert summary["resolved_reconciliations_deleted"] == 1
    assert summary["resolved_manual_reviews_deleted"] == 1

    # Verify that unresolved errors are PRESERVED
    assert error_repo.get_by_id("err-old-unresolved") is not None
    assert error_repo.get_by_id("err-old-resolved") is None

    # Verify that PENDING reconciliations and reviews are PRESERVED
    assert rec_repo.get_by_id("rec-pending-old") is not None
    assert rec_repo.get_by_id("rec-resolved-old") is None

    assert review_repo.get_by_id("rev-pending-old") is not None
    assert review_repo.get_by_id("rev-resolved-old") is None

    # Verify that an audit event was recorded
    import json
    events = event_repo.list_events(limit=50)
    ret_events = [e for e in events if e.event_code == EventCode.RETENTION_CLEANUP_COMPLETED.value]
    assert len(ret_events) >= 1
    p = json.loads(ret_events[0].payload_json) if ret_events[0].payload_json else {}
    assert p.get("success") is True
