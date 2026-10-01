"""Unit tests for DefaultWorkerManager worker orchestration, concurrency limits, and crash recovery."""

import pytest
from unittest.mock import MagicMock

from backend.workers.default_manager import DefaultWorkerManager
from backend.domain.enums import WorkerMode, WorkerStatus, TaskState, TaskType
from backend.domain.models import Task, Contact
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.task_repo import TaskRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.contact_repo import ContactRepository
from backend.config.settings import reset_settings, set_settings, AppSettings


@pytest.fixture(autouse=True)
def clean_config():
    reset_settings()
    yield
    reset_settings()


@pytest.fixture
def manager_env(tmp_path):
    db_path = str(tmp_path / "test_wkr_mgr.db")
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()

    task_repo = TaskRepository(db)
    event_repo = EventRepository(db)
    contact_repo = ContactRepository(db)

    # Pre-create test contact
    contact_repo.create(Contact(id="c-mgr", name="Manager Contact", instagram_url="https://instagram.com/c_mgr"))

    mock_browser_mgr = MagicMock()
    mock_session = MagicMock()
    mock_session.session_id = "SESS-M1"
    mock_browser_mgr.create_session.return_value = mock_session

    mock_executor = MagicMock()
    mock_executor.execute_source_sync.return_value = True

    mgr = DefaultWorkerManager(
        task_repo=task_repo,
        event_repo=event_repo,
        browser_manager=mock_browser_mgr,
        task_executor=mock_executor,
    )
    yield mgr, task_repo, event_repo, db
    mgr.shutdown_all()


def test_start_and_stop_worker(manager_env):
    mgr, task_repo, event_repo, db = manager_env
    rec = mgr.start_worker(WorkerMode.SINGLE_BROWSER)
    assert rec.status == WorkerStatus.IDLE
    assert len(mgr.list_workers()) == 1

    stopped = mgr.stop_worker(rec.id)
    assert stopped is True
    assert len(mgr.list_workers()) == 0


def test_max_workers_enforced(manager_env):
    mgr, task_repo, event_repo, db = manager_env
    set_settings(AppSettings(max_workers=1))

    mgr.start_worker(WorkerMode.SINGLE_BROWSER)
    with pytest.raises(RuntimeError, match="already at max_workers"):
        mgr.start_worker(WorkerMode.SINGLE_BROWSER)


def test_recover_stale_workers(manager_env):
    mgr, task_repo, event_repo, db = manager_env
    rec = mgr.start_worker(WorkerMode.SINGLE_BROWSER)
    worker = mgr.get_worker(rec.id)

    # Task held by worker
    task = Task(id="t-stale", contact_id="c-mgr", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    worker.claim_task("t-stale")

    # Simulate stale heartbeat
    worker.last_heartbeat = "2020-01-01T00:00:00+00:00"

    recovered = mgr.recover_stale_workers()
    assert recovered == 1

    # Held task must be marked INTERRUPTED
    t = task_repo.get_by_id("t-stale")
    assert t.status == TaskState.INTERRUPTED
    assert t.lock_token is None


def test_process_tasks_dispatch(manager_env):
    mgr, task_repo, event_repo, db = manager_env
    task = Task(id="t-disp", contact_id="c-mgr", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)

    mgr.start_worker(WorkerMode.SINGLE_BROWSER)
    processed = mgr.process_tasks(max_tasks=1)
    assert processed == 1


def test_worker_manager_obeys_browser_manager_limits(manager_env):
    mgr, task_repo, event_repo, db = manager_env
    # BrowserManager dictates max 2 workers, settings configured for 10
    mgr.browser_manager.effective_max_workers = 2
    mgr.browser_manager.effective_mode = WorkerMode.MULTI_BROWSER
    set_settings(AppSettings(max_workers=10))

    mgr.start_worker(WorkerMode.MULTI_BROWSER)
    mgr.start_worker(WorkerMode.MULTI_BROWSER)
    assert mgr.active_count == 2

    # Third worker must be rejected
    with pytest.raises(RuntimeError, match="already at max_workers=2"):
        mgr.start_worker(WorkerMode.MULTI_BROWSER)


def test_worker_manager_obeys_browser_manager_single_mode_fallback(manager_env):
    mgr, task_repo, event_repo, db = manager_env
    mgr.browser_manager.effective_max_workers = 1
    mgr.browser_manager.effective_mode = WorkerMode.SINGLE_BROWSER

    # Requesting MULTI_BROWSER must be forced to SINGLE_BROWSER with ceiling 1
    rec = mgr.start_worker(WorkerMode.MULTI_BROWSER)
    assert rec.mode == WorkerMode.SINGLE_BROWSER
    with pytest.raises(RuntimeError, match="already at max_workers=1"):
        mgr.start_worker(WorkerMode.MULTI_BROWSER)


def test_worker_manager_session_startup_failure_cleans_up(manager_env):
    mgr, task_repo, event_repo, db = manager_env
    failing_session = MagicMock()
    failing_session.session_id = "SESS-FAIL"
    failing_session.start.side_effect = RuntimeError("Browser crashed during start")
    mgr.browser_manager.create_session.return_value = failing_session

    with pytest.raises(RuntimeError, match="Browser crashed during start"):
        mgr.start_worker(WorkerMode.SINGLE_BROWSER)

    # BrowserManager's stop_session must have been called and no workers retained
    mgr.browser_manager.stop_session.assert_called_with("SESS-FAIL")
    assert mgr.active_count == 0

