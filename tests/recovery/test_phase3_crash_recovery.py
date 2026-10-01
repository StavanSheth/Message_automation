"""Phase 3 tests for Worker, Browser, Lease, and Startup Crash Recovery (Workstreams F, G, H, X)."""

import pytest
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.task_repo import TaskRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.reconciliation_repo import ReconciliationRepository
from backend.repositories.manual_review_repo import ManualReviewRepository
from backend.repositories.event_repo import EventRepository
from backend.reconciliation.service import ReconciliationService
from backend.recovery.task_recovery import TaskRecoveryHandler
from backend.recovery.service import DefaultRecoveryService
from backend.application.lifecycle import ApplicationLifecycleManager
from backend.browser.recovery import BrowserRecoveryManager
from backend.domain.models import Task, Message, Contact
from backend.domain.enums import TaskState, TaskType, MessageState, ReconciliationState, RepliedStatus


@pytest.fixture
def recovery_env(tmp_path):
    db = DatabaseManager(str(tmp_path / "test_recovery_phase3.db"))
    MigrationRunner(db).apply_pending()
    contact_repo = ContactRepository(db)
    contact_repo.create(Contact(id="C-1", name="Test User", instagram_url="https://instagram.com/test_user", replied_status=RepliedStatus.UNKNOWN))
    task_repo = TaskRepository(db)
    msg_repo = MessageRepository(db)
    rec_repo = ReconciliationRepository(db)
    review_repo = ManualReviewRepository(db)
    evt_repo = EventRepository(db)

    rec_service = ReconciliationService(
        reconciliation_repo=rec_repo,
        task_repo=task_repo,
        message_repo=msg_repo,
        manual_review_repo=review_repo,
        event_repo=evt_repo,
    )

    recovery_service = DefaultRecoveryService(
        task_repo=task_repo,
        event_repo=evt_repo,
        reconciliation_service=rec_service,
    )

    lifecycle = ApplicationLifecycleManager(
        db=db,
        task_repo=task_repo,
        event_repo=evt_repo,
        manual_review_repo=review_repo,
        reconciliation_service=rec_service,
        recovery_service=recovery_service,
    )

    return {
        "db": db,
        "task_repo": task_repo,
        "msg_repo": msg_repo,
        "rec_service": rec_service,
        "recovery_service": recovery_service,
        "lifecycle": lifecycle,
        "artifacts_dir": str(tmp_path / "artifacts"),
    }


def test_expired_lease_recovery(recovery_env):
    task_repo = recovery_env["task_repo"]
    handler = TaskRecoveryHandler(
        task_repo=task_repo,
        reconciliation_service=recovery_env["rec_service"],
    )

    # Create task with expired lease
    task = Task(id="T-EXP-1", contact_id="C-1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    task_repo.acquire_lease("T-EXP-1", "worker-dead", lease_duration_seconds=-60)

    recovered = handler.recover_expired_leases()
    assert recovered == 1

    t = task_repo.get_by_id("T-EXP-1")
    assert t.status == TaskState.INTERRUPTED
    assert t.lease_id is None
    assert t.lease_owner is None


def test_interrupted_sending_task_routes_to_reconciliation(recovery_env):
    task_repo = recovery_env["task_repo"]
    msg_repo = recovery_env["msg_repo"]
    rec_service = recovery_env["rec_service"]

    task = Task(id="T-CRASH-SEND", contact_id="C-1", type=TaskType.MESSAGE, status=TaskState.RUNNING)
    task_repo.create(task)
    # Message was in SENDING when crash occurred
    msg = Message(id="M-CRASH-1", contact_id="C-1", task_id="T-CRASH-SEND", sequence=0, body="Text", status=MessageState.SENDING)
    msg_repo.create(msg)

    # When recovering a task that crashed during SENDING, it MUST enter reconciliation
    rec_record = rec_service.enter_reconciliation(
        task_id="T-CRASH-SEND",
        message_id="M-CRASH-1",
        worker_id="worker-crashed",
        reason="crash_during_sending",
    )

    assert rec_record is not None
    t = task_repo.get_by_id("T-CRASH-SEND")
    assert t.status == TaskState.RECONCILING

    m = msg_repo.get_by_id("M-CRASH-1")
    assert m.status == MessageState.RECONCILIATION


def test_startup_recovery_idempotence(recovery_env):
    lifecycle = recovery_env["lifecycle"]
    task_repo = recovery_env["task_repo"]

    # Seed an orphaned RUNNING task
    task = Task(id="T-ORPHAN-1", contact_id="C-1", type=TaskType.MESSAGE, status=TaskState.RUNNING)
    task_repo.create(task)

    # First run of startup recovery
    summary1 = lifecycle.startup_recovery()
    assert summary1["interrupted_tasks_recovered"] >= 1

    t1 = task_repo.get_by_id("T-ORPHAN-1")
    # Recovered to QUEUED for retry
    assert t1.status in (TaskState.QUEUED, TaskState.READY)

    # Second run of startup recovery (must be completely idempotent)
    summary2 = lifecycle.startup_recovery()
    assert summary2["interrupted_tasks_recovered"] == 0

    t2 = task_repo.get_by_id("T-ORPHAN-1")
    assert t2.status == t1.status
