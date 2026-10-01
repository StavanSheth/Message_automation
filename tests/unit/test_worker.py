"""Unit tests for Worker instance lifecycle, continuous heartbeat, and task execution."""

import time
import pytest
from unittest.mock import MagicMock

from backend.workers.worker import Worker
from backend.domain.enums import WorkerMode, WorkerStatus, TaskState, TaskType
from backend.domain.models import Task, Contact
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.task_repo import TaskRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.contact_repo import ContactRepository
from tests.fixtures.mock_browser import create_mock_session


@pytest.fixture
def worker_env(tmp_path):
    db_path = str(tmp_path / "test_worker.db")
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()

    task_repo = TaskRepository(db)
    event_repo = EventRepository(db)
    contact_repo = ContactRepository(db)

    # Pre-create test contact
    contact_repo.create(Contact(id="c-wkr", name="Worker Contact", instagram_url="https://instagram.com/c_wkr"))

    session = create_mock_session("SESS-W1")
    worker = Worker(
        worker_id="WKR-01",
        worker_code="worker-1",
        mode=WorkerMode.SINGLE_BROWSER,
        task_repo=task_repo,
        event_repo=event_repo,
        session=session,
        heartbeat_interval=1,
        stale_timeout=3,
    )
    yield worker, task_repo, event_repo, db
    worker.stop()


def test_worker_start_and_stop(worker_env):
    worker, task_repo, event_repo, db = worker_env
    worker.start()
    assert worker.status == WorkerStatus.IDLE
    assert worker.session.status.value in ("READY", "BUSY")

    worker.stop()
    assert worker.status == WorkerStatus.STOPPED
    assert worker._heartbeat_thread is None


def test_worker_continuous_heartbeat(worker_env):
    worker, task_repo, event_repo, db = worker_env
    worker.start()
    initial_hb = worker.last_heartbeat

    # Wait for background heartbeat thread to pulse
    time.sleep(1.2)
    new_hb = worker.last_heartbeat
    assert new_hb >= initial_hb


def test_worker_claim_and_release_task(worker_env):
    worker, task_repo, event_repo, db = worker_env
    task = Task(id="t-w1", contact_id="c-wkr", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)

    worker.start()
    claimed = worker.claim_task("t-w1")
    assert claimed is True
    assert worker.status == WorkerStatus.BUSY
    assert worker.current_task_id == "t-w1"

    worker.release_current_task()
    assert worker.status == WorkerStatus.IDLE
    assert worker.current_task_id is None


def test_worker_process_next_task_success(worker_env):
    worker, task_repo, event_repo, db = worker_env
    task = Task(id="t-proc", contact_id="c-wkr", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)

    mock_executor = MagicMock()
    mock_executor.execute_source_sync.return_value = True

    worker.start()
    success = worker.process_next_task(executor=mock_executor)

    assert success is True
    assert worker.status == WorkerStatus.IDLE
    assert worker.current_task_id is None
    mock_executor.execute_source_sync.assert_called_once()


def test_worker_process_next_task_exception_returns_to_idle(worker_env):
    worker, task_repo, event_repo, db = worker_env
    task = Task(id="t-err", contact_id="c-wkr", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)

    mock_executor = MagicMock()
    mock_executor.execute_source_sync.side_effect = RuntimeError("Simulated crash")

    worker.start()
    success = worker.process_next_task(executor=mock_executor)

    assert success is False
    # Invariant: worker is NEVER left permanently BUSY
    assert worker.status == WorkerStatus.IDLE
    assert worker.current_task_id is None
