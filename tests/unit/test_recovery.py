"""Unit tests for DefaultRecoveryService authoritative recovery, reconciliation, and crash recovery."""

import pytest
from backend.recovery.service import DefaultRecoveryService
from backend.domain.enums import TaskState, TaskType
from backend.domain.models import Task, Contact
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.task_repo import TaskRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.error_repo import ErrorRepository
from backend.repositories.contact_repo import ContactRepository


@pytest.fixture
def recovery_env(tmp_path):
    db_path = str(tmp_path / "test_rec.db")
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()

    task_repo = TaskRepository(db)
    event_repo = EventRepository(db)
    error_repo = ErrorRepository(db)
    contact_repo = ContactRepository(db)

    contact_repo.create(Contact(id="c-rec", name="Rec Contact", instagram_url="https://instagram.com/c_rec"))

    service = DefaultRecoveryService(
        task_repo=task_repo,
        event_repo=event_repo,
        error_repo=error_repo,
    )
    yield service, task_repo, event_repo, db


def test_enter_reconciliation(recovery_env):
    service, task_repo, event_repo, db = recovery_env
    task = Task(id="t-r1", contact_id="c-rec", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    task_repo.claim_task("t-r1", "w1", "lock-r1")

    rec_task = service.enter_reconciliation("t-r1", reason="browser_disconnect")
    assert rec_task.status == TaskState.RECONCILING


def test_reconcile_confirmed_to_completed(recovery_env):
    service, task_repo, _, db = recovery_env
    task = Task(id="t-r2", contact_id="c-rec", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    task_repo.claim_task("t-r2", "w1", "lock-r2")
    service.enter_reconciliation("t-r2")

    result = service.reconcile_task("t-r2", verification_confirmed=True, details="confirmed sent")
    assert result.status == TaskState.COMPLETED


def test_reconcile_unconfirmed_to_manual_review(recovery_env):
    service, task_repo, _, db = recovery_env
    task = Task(id="t-r3", contact_id="c-rec", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    task_repo.claim_task("t-r3", "w1", "lock-r3")
    service.enter_reconciliation("t-r3")

    result = service.reconcile_task("t-r3", verification_confirmed=None, details="indeterminate state")
    assert result.status == TaskState.MANUAL_REVIEW


def test_recover_interrupted_tasks_requeues_low_attempts(recovery_env):
    service, task_repo, _, db = recovery_env
    task = Task(id="t-r4", contact_id="c-rec", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    task_repo.claim_task("t-r4", "w1", "lock-r4")
    task_repo.mark_running_as_interrupted()

    recovered = service.recover_interrupted_tasks(max_retries=3)
    assert len(recovered) == 1
    assert recovered[0].status == TaskState.QUEUED


def test_recover_interrupted_tasks_escalates_high_attempts(recovery_env):
    service, task_repo, _, db = recovery_env
    task = Task(id="t-r5", contact_id="c-rec", type=TaskType.MESSAGE, status=TaskState.READY, attempt_count=3)
    task_repo.create(task)
    task_repo.claim_task("t-r5", "w1", "lock-r5")
    task_repo.mark_running_as_interrupted()

    recovered = service.recover_interrupted_tasks(max_retries=3)
    assert len(recovered) == 1
    assert recovered[0].status == TaskState.MANUAL_REVIEW


def test_recover_crashed_worker(recovery_env):
    service, task_repo, _, db = recovery_env
    task = Task(id="t-r6", contact_id="c-rec", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    task_repo.claim_task("t-r6", "w-crash", "lock-w-crash")

    count = service.recover_crashed_worker("w-crash")
    assert count == 1
    t = task_repo.get_by_id("t-r6")
    assert t.status == TaskState.INTERRUPTED
    assert t.lock_token is None
