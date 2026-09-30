"""Phase 2 integration tests: concurrent claiming, task execution lifecycle, browser source sync."""

import pytest
import threading
from unittest.mock import MagicMock

from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.task_repo import TaskRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.error_repo import ErrorRepository
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.source_record_repo import SourceRecordRepository
from backend.domain.models import Task, Contact, utc_now_iso
from backend.domain.enums import TaskType, TaskState, WorkerMode
from backend.config.settings import AppSettings, reset_settings, set_settings
from backend.events.correlation import reset_counters


@pytest.fixture(autouse=True)
def clean(tmp_path):
    reset_settings()
    reset_counters()
    yield
    reset_settings()
    reset_counters()


def _setup_db(tmp_path, name="test_phase2_int.db"):
    db_path = str(tmp_path / name)
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()
    return db


def _create_task(db, task):
    contact_repo = ContactRepository(db)
    if not contact_repo.get_by_id(task.contact_id):
        contact_repo.create(Contact(id=task.contact_id, name=f"Contact {task.contact_id}", instagram_url=f"https://instagram.com/{task.contact_id}"))
    task_repo = TaskRepository(db)
    return task_repo.create(task)


# ── Concurrent Task Claiming ──────────────────────────────────────────────

class TestConcurrentClaiming:
    """Two workers attempting the same task: only one may succeed."""

    def test_two_workers_cannot_claim_same_task(self, tmp_path):
        db = _setup_db(tmp_path)
        task_repo = TaskRepository(db)

        task = Task(id="t-race", contact_id="c-race", type=TaskType.MESSAGE, status=TaskState.READY)
        _create_task(db, task)

        results = []

        def claim_worker(worker_id, lock_token):
            res = task_repo.claim_task("t-race", worker_id, lock_token)
            results.append((worker_id, res))

        t1 = threading.Thread(target=claim_worker, args=("w1", "lock-A"))
        t2 = threading.Thread(target=claim_worker, args=("w2", "lock-B"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        successes = [r for r in results if r[1] is True]
        failures = [r for r in results if r[1] is False]
        assert len(successes) == 1, f"Exactly one worker should claim: {results}"
        assert len(failures) == 1

    def test_lock_owner_enforced(self, tmp_path):
        db = _setup_db(tmp_path)
        task_repo = TaskRepository(db)

        task = Task(id="t-lock", contact_id="c-lock", type=TaskType.MESSAGE, status=TaskState.READY)
        _create_task(db, task)
        task_repo.claim_task("t-lock", "w1", "correct-lock")

        # Wrong lock_token should fail release
        assert task_repo.release_task("t-lock", "wrong-lock") is False
        # Correct lock_token should succeed
        assert task_repo.release_task("t-lock", "correct-lock") is True

    def test_claimed_task_blocks_reclaim(self, tmp_path):
        db = _setup_db(tmp_path)
        task_repo = TaskRepository(db)

        task = Task(id="t-block", contact_id="c-block", type=TaskType.MESSAGE, status=TaskState.READY)
        _create_task(db, task)
        assert task_repo.claim_task("t-block", "w1", "lock-1") is True
        assert task_repo.claim_task("t-block", "w2", "lock-2") is False


# ── Task Execution Lifecycle ──────────────────────────────────────────────

class TestTaskExecutionLifecycle:

    def test_full_lifecycle_claim_execute_complete(self, tmp_path):
        """READY → claim → RUNNING → COMPLETED with lock release."""
        db = _setup_db(tmp_path)
        task_repo = TaskRepository(db)
        event_repo = EventRepository(db)
        error_repo = ErrorRepository(db)

        task = Task(id="t-life", contact_id="c-life", type=TaskType.MESSAGE, status=TaskState.READY)
        _create_task(db, task)

        # Claim
        assert task_repo.claim_task("t-life", "w1", "lock-life") is True
        t = task_repo.get_by_id("t-life")
        assert t.status == TaskState.RUNNING
        assert t.lock_token == "lock-life"
        assert t.worker_id == "w1"
        assert t.attempt_count == 1

        # Complete
        task_repo.update_state("t-life", TaskState.COMPLETED, worker_id="w1")
        task_repo.release_task("t-life", "lock-life")
        t = task_repo.get_by_id("t-life")
        assert t.status == TaskState.COMPLETED
        assert t.lock_token is None

    def test_failed_task_recovery_lifecycle(self, tmp_path):
        """READY → claim → RUNNING → FAILED → release → READY (manual retry)."""
        db = _setup_db(tmp_path)
        task_repo = TaskRepository(db)

        task = Task(id="t-fail", contact_id="c-fail", type=TaskType.MESSAGE, status=TaskState.READY)
        _create_task(db, task)
        task_repo.claim_task("t-fail", "w1", "lock-fail")
        task_repo.update_state("t-fail", TaskState.FAILED, worker_id="w1")
        task_repo.release_task("t-fail", "lock-fail")

        t = task_repo.get_by_id("t-fail")
        assert t.status == TaskState.FAILED
        assert t.lock_token is None

        # Manual retry: FAILED → READY
        task_repo.update_state("t-fail", TaskState.READY)
        t = task_repo.get_by_id("t-fail")
        assert t.status == TaskState.READY


# ── Crash Recovery ────────────────────────────────────────────────────────

class TestCrashRecovery:

    def test_running_tasks_marked_interrupted_on_startup(self, tmp_path):
        db = _setup_db(tmp_path)
        task_repo = TaskRepository(db)

        t1 = Task(id="t-cr1", contact_id="c1", type=TaskType.MESSAGE, status=TaskState.READY)
        t2 = Task(id="t-cr2", contact_id="c2", type=TaskType.MESSAGE, status=TaskState.READY)
        _create_task(db, t1)
        _create_task(db, t2)
        task_repo.claim_task("t-cr1", "w1", "lock-cr1")
        task_repo.claim_task("t-cr2", "w2", "lock-cr2")

        # Simulate crash: both tasks are RUNNING
        count = task_repo.mark_running_as_interrupted()
        assert count == 2

        for tid in ["t-cr1", "t-cr2"]:
            t = task_repo.get_by_id(tid)
            assert t.status == TaskState.INTERRUPTED
            assert t.lock_token is None

    def test_reconciliation_after_crash(self, tmp_path):
        from backend.automation.recovery import TaskReconciliationService
        db = _setup_db(tmp_path)
        task_repo = TaskRepository(db)
        event_repo = EventRepository(db)
        error_repo = ErrorRepository(db)

        task = Task(id="t-recon", contact_id="c-recon", type=TaskType.MESSAGE, status=TaskState.READY)
        _create_task(db, task)
        task_repo.claim_task("t-recon", "w1", "lock-recon")
        task_repo.mark_running_as_interrupted()

        recovery = TaskReconciliationService(task_repo, event_repo, error_repo)
        recovered = recovery.recover_interrupted_tasks()
        assert len(recovered) == 1
        assert recovered[0].status == TaskState.QUEUED

    def test_reconcile_unknown_to_manual_review(self, tmp_path):
        from backend.automation.recovery import TaskReconciliationService
        db = _setup_db(tmp_path)
        task_repo = TaskRepository(db)
        event_repo = EventRepository(db)
        error_repo = ErrorRepository(db)

        task = Task(id="t-unkn", contact_id="c-unkn", type=TaskType.MESSAGE, status=TaskState.READY)
        _create_task(db, task)
        task_repo.claim_task("t-unkn", "w1", "lock-unkn")
        # Simulate unknown result → RECONCILING
        task_repo.update_state("t-unkn", TaskState.RECONCILING)

        recovery = TaskReconciliationService(task_repo, event_repo, error_repo)
        result = recovery.reconcile_task("t-unkn", verification_confirmed=None)
        assert result.status == TaskState.MANUAL_REVIEW

    def test_reconcile_confirmed_to_completed(self, tmp_path):
        from backend.automation.recovery import TaskReconciliationService
        db = _setup_db(tmp_path)
        task_repo = TaskRepository(db)
        event_repo = EventRepository(db)
        error_repo = ErrorRepository(db)

        task = Task(id="t-conf", contact_id="c-conf", type=TaskType.MESSAGE, status=TaskState.READY)
        _create_task(db, task)
        task_repo.claim_task("t-conf", "w1", "lock-conf")
        task_repo.update_state("t-conf", TaskState.RECONCILING)

        recovery = TaskReconciliationService(task_repo, event_repo, error_repo)
        result = recovery.reconcile_task("t-conf", verification_confirmed=True, details="message confirmed sent")
        assert result.status == TaskState.COMPLETED
