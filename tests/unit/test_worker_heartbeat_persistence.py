"""
Unit tests for Worker Heartbeat Persistence & Stale Worker Recovery.
Verifies that worker state and heartbeats are persisted in WorkerRepository
and that stale workers are recovered properly.
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock
import pytest

from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.domain.models import WorkerRecord, Task, Contact
from backend.domain.enums import WorkerStatus, WorkerMode, TaskType, TaskState
from backend.repositories.worker_repo import WorkerRepository
from backend.repositories.task_repo import TaskRepository
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.event_repo import EventRepository
from backend.workers.worker import Worker
from backend.workers.default_manager import DefaultWorkerManager


@pytest.fixture
def db(tmp_path):
    db_file = str(tmp_path / "test_worker_hb.db")
    mgr = DatabaseManager(db_file)
    runner = MigrationRunner(mgr)
    runner.apply_pending()
    return mgr


def test_worker_registration_and_heartbeat_persistence(db):
    worker_repo = WorkerRepository(db)
    task_repo = TaskRepository(db)
    event_repo = EventRepository(db)
    contact_repo = ContactRepository(db)

    contact_repo.create(Contact(id="test-contact-1", name="HB Contact", instagram_url="https://instagram.com/hb_c"))

    worker = Worker(
        worker_id="worker-persist-1",
        worker_code="W-001",
        mode=WorkerMode.SINGLE_BROWSER,
        task_repo=task_repo,
        event_repo=event_repo,
        worker_repo=worker_repo,
        heartbeat_interval=1,
    )

    # Initially on init, worker record is created in repo
    rec = worker_repo.get_by_id("worker-persist-1")
    assert rec is not None
    assert rec.status == WorkerStatus.IDLE

    # Worker starts
    worker.start()
    assert worker.status == WorkerStatus.IDLE

    # Create task in READY state so it can be claimed
    task = Task(
        id="task-hb-1",
        contact_id="test-contact-1",
        type=TaskType.MESSAGE,
        status=TaskState.READY,
        scheduled_at=datetime.now(timezone.utc).isoformat(),
    )
    task_repo.create(task)

    claimed = worker.claim_task("task-hb-1")
    assert claimed is True
    assert worker.status == WorkerStatus.BUSY

    # Verify worker repo updated
    rec_after_claim = worker_repo.get_by_id("worker-persist-1")
    assert rec_after_claim.status == WorkerStatus.BUSY
    assert rec_after_claim.current_task_id == "task-hb-1"

    # Release task
    worker.release_current_task()
    assert worker.status == WorkerStatus.IDLE

    rec_after_release = worker_repo.get_by_id("worker-persist-1")
    assert rec_after_release.status == WorkerStatus.IDLE
    assert rec_after_release.current_task_id is None

    worker.stop()
    rec_after_stop = worker_repo.get_by_id("worker-persist-1")
    assert rec_after_stop.status == WorkerStatus.STOPPED


def test_stale_worker_recovery(db):
    worker_repo = WorkerRepository(db)
    task_repo = TaskRepository(db)
    contact_repo = ContactRepository(db)
    event_repo = EventRepository(db)

    contact_repo.create(Contact(id="c-1", name="Stale Contact", instagram_url="https://instagram.com/stale_c"))

    # Pre-create a task in task_repo
    task = Task(
        id="stale-task-1",
        contact_id="c-1",
        type=TaskType.MESSAGE,
        status=TaskState.RUNNING,
        scheduled_at=datetime.now(timezone.utc).isoformat(),
        worker_id="stale-worker-1",
        lock_token="lock-123",
        locked_at=datetime.now(timezone.utc).isoformat(),
    )
    task_repo.create(task)

    # Pre-create a stale worker record in DB whose last heartbeat was 100 seconds ago
    stale_time = (datetime.now(timezone.utc) - timedelta(seconds=100)).isoformat()
    worker_rec = WorkerRecord(
        id="stale-worker-1",
        worker_code="W-STALE",
        mode=WorkerMode.SINGLE_BROWSER,
        status=WorkerStatus.BUSY,
        current_task_id="stale-task-1",
        last_heartbeat=stale_time,
    )
    worker_repo.create(worker_rec)

    manager = DefaultWorkerManager(
        task_repo=task_repo,
        event_repo=event_repo,
        worker_repo=worker_repo,
    )

    # Recover stale workers
    recovered_count = manager.recover_stale_workers()
    assert recovered_count >= 1

    # Verify worker record marked CRASHED in DB
    updated_worker = worker_repo.get_by_id("stale-worker-1")
    assert updated_worker.status == WorkerStatus.CRASHED

    # Verify task state marked INTERRUPTED and unlocked in DB
    updated_task = task_repo.get_by_id("stale-task-1")
    assert updated_task.status == TaskState.INTERRUPTED
    assert updated_task.lock_token is None
