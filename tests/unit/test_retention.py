"""Unit tests for Phase 4 retention service pruning expired data while preserving active records."""

import pytest
from datetime import datetime, timezone, timedelta
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.application.retention_service import RetentionService
from backend.repositories.event_repo import EventRepository
from backend.repositories.task_repo import TaskRepository
from backend.repositories.manual_review_repo import ManualReviewRepository
from backend.repositories.contact_repo import ContactRepository
from backend.domain.models import Task, ManualReviewItem, Contact, utc_now_iso
from backend.domain.enums import TaskState, TaskType, EventCode, EventLevel


@pytest.fixture
def retention_env(tmp_path):
    db_path = str(tmp_path / "test_retention.db")
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()

    service = RetentionService(db)
    event_repo = EventRepository(db)
    task_repo = TaskRepository(db)
    review_repo = ManualReviewRepository(db)
    contact_repo = ContactRepository(db)

    contact_repo.create(Contact(id="c-ret-1", name="Ret Contact", instagram_url="https://instagram.com/ret1"))

    return {
        "db": db,
        "service": service,
        "event_repo": event_repo,
        "task_repo": task_repo,
        "review_repo": review_repo,
    }


def test_retention_preserves_active_records(retention_env):
    service = retention_env["service"]
    event_repo = retention_env["event_repo"]
    review_repo = retention_env["review_repo"]
    db = retention_env["db"]

    old_ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    recent_ts = utc_now_iso()

    # 1. Old event vs fresh event
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO events (id, timestamp, event_code, category, level, payload_json) VALUES (?, ?, ?, ?, ?, ?);",
            ("ev-old", old_ts, EventCode.TASK_STARTED.value, "execution", EventLevel.INFO.value, "{}"),
        )
        conn.execute(
            "INSERT INTO events (id, timestamp, event_code, category, level, payload_json) VALUES (?, ?, ?, ?, ?, ?);",
            ("ev-fresh", recent_ts, EventCode.TASK_STARTED.value, "execution", EventLevel.INFO.value, "{}"),
        )

        # 2. Parent task for foreign keys
        task_repo = retention_env["task_repo"]
        task_repo.create(Task(id="t1", contact_id="c-ret-1", type=TaskType.MESSAGE, status=TaskState.READY))

        # 3. Pending review (old) vs resolved review (old)
        conn.execute(
            """
            INSERT INTO manual_reviews (
                id, task_id, contact_id, reason, current_state, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            ("rev-pending-old", "t1", "c-ret-1", "Audit", "SENDING", "PENDING", old_ts),
        )
        conn.execute(
            """
            INSERT INTO manual_reviews (
                id, task_id, contact_id, reason, current_state, status, created_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            ("rev-resolved-old", "t1", "c-ret-1", "Audit", "SENDING", "RESOLVED", old_ts, old_ts),
        )

    # Run retention cleanup with 30-day window
    summary = service.cleanup_expired_data(event_retention_days=30, resolved_retention_days=30)
    assert summary["events_deleted"] == 1
    assert summary["resolved_manual_reviews_deleted"] == 1

    conn = db.get_connection()
    # Check that fresh event remains
    cur = conn.execute("SELECT id FROM events WHERE id = 'ev-fresh';")
    assert cur.fetchone() is not None

    # Check that pending manual review was NOT deleted despite being old
    cur = conn.execute("SELECT id FROM manual_reviews WHERE id = 'rev-pending-old';")
    assert cur.fetchone() is not None
