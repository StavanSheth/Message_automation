"""Recovery tests for Phase 4 startup reconciliation, orphaned identities, and idempotent recovery."""

import pytest
from datetime import datetime, timezone, timedelta
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.task_repo import TaskRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.reconciliation_repo import ReconciliationRepository
from backend.repositories.manual_review_repo import ManualReviewRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.execution_identity_repo import ExecutionIdentityRepository
from backend.application.lifecycle import ApplicationLifecycleManager
from backend.reconciliation.service import ReconciliationService
from backend.domain.models import Task, Message, ManualReviewItem, utc_now_iso
from backend.domain.enums import TaskState, TaskType, MessageState, ReconciliationState


@pytest.fixture
def recovery_env(tmp_path):
    db_path = str(tmp_path / "test_recovery_p4.db")
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()

    task_repo = TaskRepository(db)
    msg_repo = MessageRepository(db)
    rec_repo = ReconciliationRepository(db)
    review_repo = ManualReviewRepository(db)
    event_repo = EventRepository(db)
    exec_id_repo = ExecutionIdentityRepository(db)

    rec_svc = ReconciliationService(
        reconciliation_repo=rec_repo,
        task_repo=task_repo,
        message_repo=msg_repo,
        manual_review_repo=review_repo,
        event_repo=event_repo,
    )

    lifecycle = ApplicationLifecycleManager(
        db=db,
        task_repo=task_repo,
        event_repo=event_repo,
        manual_review_repo=review_repo,
        reconciliation_service=rec_svc,
    )

    return {
        "db": db,
        "lifecycle": lifecycle,
        "task_repo": task_repo,
        "msg_repo": msg_repo,
        "rec_repo": rec_repo,
        "review_repo": review_repo,
        "rec_svc": rec_svc,
        "exec_id_repo": exec_id_repo,
    }


def test_crash_during_send_routes_to_reconciliation_on_startup(recovery_env):
    lifecycle = recovery_env["lifecycle"]
    task_repo = recovery_env["task_repo"]
    rec_repo = recovery_env["rec_repo"]
    db = recovery_env["db"]

    from backend.repositories.contact_repo import ContactRepository
    from backend.domain.models import Contact
    contact_repo = ContactRepository(db)
    contact_repo.create(Contact(id="c1", name="Test Rec", instagram_url="https://instagram.com/rec1"))

    # Simulate a task left in SENDING with an expired lease when process crashed
    expired_time = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    task = Task(
        id="t-crashed-send",
        contact_id="c1",
        type=TaskType.MESSAGE,
        status=TaskState.SENDING,
        lease_id="LEASE-CRASH",
        lock_token="LEASE-CRASH",
        lease_expires_at=expired_time,
        worker_id="WKR-CRASHED",
    )
    task_repo.create(task)

    # 1st Startup recovery execution
    summary1 = lifecycle.startup_recovery()
    assert summary1["expired_leases_recovered"] >= 1

    t = task_repo.get_by_id("t-crashed-send")
    assert t.status == TaskState.RECONCILING

    rec_record = rec_repo.get_by_task_id("t-crashed-send")
    assert rec_record is not None
    assert rec_record.state == ReconciliationState.PENDING.value

    # 2nd Startup recovery execution (Idempotency test)
    summary2 = lifecycle.startup_recovery()
    assert summary2["expired_leases_recovered"] == 0

    # Ensure no duplicate reconciliation records created
    conn = db.get_connection()
    cur = conn.execute("SELECT COUNT(*) FROM reconciliations WHERE task_id = 't-crashed-send';")
    assert cur.fetchone()[0] == 1


def test_startup_detects_pending_manual_reviews_and_orphaned_identities(recovery_env):
    lifecycle = recovery_env["lifecycle"]
    review_repo = recovery_env["review_repo"]
    db = recovery_env["db"]

    from backend.repositories.contact_repo import ContactRepository
    from backend.domain.models import Contact
    contact_repo = ContactRepository(db)
    contact_repo.create(Contact(id="c1", name="Test Rec", instagram_url="https://instagram.com/rec1"))

    # Create task and pending manual review
    task_repo = recovery_env["task_repo"]
    task_repo.create(Task(id="t-rev-check", contact_id="c1", type=TaskType.MESSAGE, status=TaskState.MANUAL_REVIEW))

    review_repo.create(
        ManualReviewItem(
            id="REV-P4-1",
            task_id="t-rev-check",
            contact_id="c1",
            reason="Ambiguous send verification",
            current_state="SENDING",
            status="PENDING",
        )
    )

    from backend.domain.models import ExecutionIdentity
    exec_id_repo = recovery_env["exec_id_repo"]
    exec_id_repo.create(
        ExecutionIdentity(
            execution_key="key-orphaned-1",
            contact_id="c1",
            task_id="t-rev-check",
            message_hash="hash1",
            attempt=1,
            worker_id="WKR-DEAD",
            session_id="SESS-DEAD",
            correlation_id="CORR-DEAD",
            state="RUNNING",
        )
    )

    summary = lifecycle.startup_recovery()
    assert summary["manual_review_items"] >= 1
    assert summary["orphaned_execution_identities"] >= 1

    # Verify orphaned identity moved out of RUNNING
    conn = db.get_connection()
    cur = conn.execute("SELECT state FROM execution_identities WHERE execution_key = 'key-orphaned-1';")
    assert cur.fetchone()[0] == "RECONCILIATION"
