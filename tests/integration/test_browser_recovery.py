"""Integration test: Browser and worker recovery and task reconciliation.

Tests:
A. Crashed worker stale heartbeat recovery -> worker CRASHED, task INTERRUPTED, lock released, WORKER_CRASHED event
B. Interrupted task with retries remaining -> QUEUED, TASK_RETRY_SCHEDULED event
C. Interrupted task exceeding retry limit -> MANUAL_REVIEW
D. Reconciliation outcomes:
   - RECONCILING + True -> COMPLETED
   - RECONCILING + False -> FAILED
   - RECONCILING + None -> MANUAL_REVIEW
E. Unknown execution result fails closed -> MANUAL_REVIEW unless confirmed
"""

import pytest

from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.task_repo import TaskRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.error_repo import ErrorRepository
from backend.repositories.contact_repo import ContactRepository
from backend.workers.default_manager import DefaultWorkerManager
from backend.recovery.service import DefaultRecoveryService
from backend.domain.models import Task, Contact
from backend.domain.enums import (
    WorkerMode,
    WorkerStatus,
    TaskState,
    TaskType,
    EventCode,
)
from backend.config.settings import reset_settings
from backend.events.correlation import reset_counters


@pytest.fixture(autouse=True)
def clean_env():
    reset_settings()
    reset_counters()
    yield
    reset_settings()
    reset_counters()


@pytest.fixture
def recovery_env(tmp_path):
    db_path = str(tmp_path / "browser_recovery_test.db")
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()

    task_repo = TaskRepository(db)
    event_repo = EventRepository(db)
    error_repo = ErrorRepository(db)
    contact_repo = ContactRepository(db)

    # Pre-create test contact
    contact_repo.create(Contact(id="c-rec-test", name="Recovery Test Contact", instagram_url="https://instagram.com/rec_test"))

    worker_manager = DefaultWorkerManager(
        task_repo=task_repo,
        event_repo=event_repo,
    )

    recovery_service = DefaultRecoveryService(
        task_repo=task_repo,
        event_repo=event_repo,
        error_repo=error_repo,
    )

    yield {
        "db": db,
        "task_repo": task_repo,
        "event_repo": event_repo,
        "error_repo": error_repo,
        "worker_manager": worker_manager,
        "recovery_service": recovery_service,
    }

    worker_manager.shutdown_all()


def test_crashed_stale_worker_recovery(recovery_env):
    """A. Crashed worker: stale heartbeat -> worker CRASHED, task INTERRUPTED, lock released, event emitted."""
    wm = recovery_env["worker_manager"]
    task_repo = recovery_env["task_repo"]
    event_repo = recovery_env["event_repo"]

    # Start worker and assign a task
    rec = wm.start_worker(WorkerMode.SINGLE_BROWSER)
    worker = wm.get_worker(rec.id)

    task = Task(id="t-stale-rec", contact_id="c-rec-test", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)

    claimed = worker.claim_task("t-stale-rec")
    assert claimed is True
    assert worker.status == WorkerStatus.BUSY

    # Simulate stale heartbeat (old timestamp)
    worker.last_heartbeat = "2020-01-01T00:00:00+00:00"

    recovered_count = wm.recover_stale_workers()
    assert recovered_count == 1

    # Verify task transitioned to INTERRUPTED and lock released
    interrupted_task = task_repo.get_by_id("t-stale-rec")
    assert interrupted_task.status == TaskState.INTERRUPTED
    assert interrupted_task.lock_token is None

    # Verify WORKER_CRASHED event
    events = event_repo.list_events(limit=50)
    crash_events = [e for e in events if e.event_code == EventCode.WORKER_CRASHED]
    assert len(crash_events) >= 1
    assert crash_events[0].entity_id == rec.id


def test_interrupted_task_with_retries_remaining(recovery_env):
    """B. Interrupted task with retries remaining: INTERRUPTED -> QUEUED + TASK_RETRY_SCHEDULED emitted."""
    task_repo = recovery_env["task_repo"]
    event_repo = recovery_env["event_repo"]
    recovery = recovery_env["recovery_service"]

    # Task with attempt_count = 1 (< max 3)
    task = Task(
        id="t-retry-ok",
        contact_id="c-rec-test",
        type=TaskType.MESSAGE,
        status=TaskState.READY,
    )
    task_repo.create(task)
    task_repo.claim_task("t-retry-ok", "w1", "lock-1")
    task_repo.mark_running_as_interrupted()

    # Verify state is INTERRUPTED
    t = task_repo.get_by_id("t-retry-ok")
    assert t.status == TaskState.INTERRUPTED
    assert t.attempt_count == 1

    # Recover
    recovered = recovery.recover_interrupted_tasks(max_retries=3)
    assert len(recovered) == 1
    assert recovered[0].id == "t-retry-ok"
    assert recovered[0].status == TaskState.QUEUED

    # Check TASK_RETRY_SCHEDULED event
    events = event_repo.list_events(limit=50)
    retry_events = [e for e in events if e.event_code == EventCode.TASK_RETRY_SCHEDULED]
    assert len(retry_events) >= 1
    assert retry_events[0].entity_id == "t-retry-ok"


def test_interrupted_task_exceeding_retry_limit(recovery_env):
    """C. Interrupted task exceeding retry limit: INTERRUPTED -> MANUAL_REVIEW."""
    task_repo = recovery_env["task_repo"]
    recovery = recovery_env["recovery_service"]

    task = Task(
        id="t-exceed-retries",
        contact_id="c-rec-test",
        type=TaskType.MESSAGE,
        status=TaskState.READY,
        attempt_count=3,
    )
    task_repo.create(task)

    # Claim and interrupt task
    task_repo.claim_task("t-exceed-retries", "w1", "lock-1")
    task_repo.mark_running_as_interrupted()
    t = task_repo.get_by_id("t-exceed-retries")
    assert t.attempt_count >= 3

    # Recover with max_retries=3 -> escalates to MANUAL_REVIEW
    recovered = recovery.recover_interrupted_tasks(max_retries=3)
    assert len(recovered) == 1
    assert recovered[0].id == "t-exceed-retries"
    assert recovered[0].status == TaskState.MANUAL_REVIEW


def test_reconciliation_confirmed_to_completed(recovery_env):
    """D1. Reconciliation: RECONCILING + True -> COMPLETED."""
    task_repo = recovery_env["task_repo"]
    recovery = recovery_env["recovery_service"]

    task = Task(id="t-recon-true", contact_id="c-rec-test", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    task_repo.claim_task("t-recon-true", "w1", "lock-1")
    task_repo.update_state("t-recon-true", TaskState.RECONCILING)

    result = recovery.reconcile_task("t-recon-true", verification_confirmed=True, details="Positive match")
    assert result.status == TaskState.COMPLETED

    saved = task_repo.get_by_id("t-recon-true")
    assert saved.status == TaskState.COMPLETED


def test_reconciliation_negative_to_failed(recovery_env):
    """D2. Reconciliation: RECONCILING + False -> FAILED."""
    task_repo = recovery_env["task_repo"]
    recovery = recovery_env["recovery_service"]

    task = Task(id="t-recon-false", contact_id="c-rec-test", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    task_repo.claim_task("t-recon-false", "w1", "lock-1")
    task_repo.update_state("t-recon-false", TaskState.RECONCILING)

    result = recovery.reconcile_task("t-recon-false", verification_confirmed=False, details="Not sent")
    assert result.status == TaskState.FAILED

    saved = task_repo.get_by_id("t-recon-false")
    assert saved.status == TaskState.FAILED


def test_reconciliation_indeterminate_to_manual_review(recovery_env):
    """D3. Reconciliation: RECONCILING + None -> MANUAL_REVIEW."""
    task_repo = recovery_env["task_repo"]
    recovery = recovery_env["recovery_service"]

    task = Task(id="t-recon-none", contact_id="c-rec-test", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    task_repo.claim_task("t-recon-none", "w1", "lock-1")
    task_repo.update_state("t-recon-none", TaskState.RECONCILING)

    result = recovery.reconcile_task("t-recon-none", verification_confirmed=None, details="No evidence")
    assert result.status == TaskState.MANUAL_REVIEW

    saved = task_repo.get_by_id("t-recon-none")
    assert saved.status == TaskState.MANUAL_REVIEW


def test_unknown_execution_fails_closed_to_manual_review(recovery_env):
    """E. Unknown execution result fails closed to MANUAL_REVIEW without positive confirmation."""
    task_repo = recovery_env["task_repo"]
    recovery = recovery_env["recovery_service"]

    task = Task(id="t-fail-closed", contact_id="c-rec-test", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    task_repo.claim_task("t-fail-closed", "w1", "lock-1")
    task_repo.update_state("t-fail-closed", TaskState.RECONCILING)

    outcome = recovery.reconcile_unknown_send("t-fail-closed", verification_confirmed=None)
    assert outcome == TaskState.MANUAL_REVIEW.value

    t = task_repo.get_by_id("t-fail-closed")
    assert t.status == TaskState.MANUAL_REVIEW
