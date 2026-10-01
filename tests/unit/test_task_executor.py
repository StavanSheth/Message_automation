"""Unit tests for TaskExecutor claim guards, lifecycle state transitions, lock release, and failure handling."""

import pytest
from unittest.mock import MagicMock

from backend.automation.task_executor import TaskExecutor
from backend.automation.execution_context import ExecutionContext
from backend.domain.enums import TaskState, TaskType, ErrorCode
from backend.domain.models import Task, Contact
from backend.domain.errors import SourceAccessError, ConflictError
from backend.browser.exceptions import BrowserTimeoutError, BrowserCrashError
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.task_repo import TaskRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.error_repo import ErrorRepository
from backend.repositories.contact_repo import ContactRepository


@pytest.fixture
def executor_env(tmp_path):
    db_path = str(tmp_path / "test_executor.db")
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()

    task_repo = TaskRepository(db)
    event_repo = EventRepository(db)
    error_repo = ErrorRepository(db)
    contact_repo = ContactRepository(db)

    contact_repo.create(Contact(id="c-exec", name="Exec Contact", instagram_url="https://instagram.com/c_exec"))

    mock_source_service = MagicMock()
    mock_sync_run = MagicMock()
    mock_sync_run.records_read = 5
    mock_sync_run.records_written = 2
    mock_sync_run.conflicts = 0
    mock_source_service.sync_source.return_value = mock_sync_run

    executor = TaskExecutor(
        task_repo=task_repo,
        event_repo=event_repo,
        error_repo=error_repo,
        source_service=mock_source_service,
    )
    yield executor, task_repo, event_repo, error_repo, mock_source_service, db


def test_executor_rejects_unclaimed_task(executor_env):
    executor, task_repo, _, _, _, db = executor_env
    task = Task(id="t-unclaimed", contact_id="c-exec", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)

    ctx = ExecutionContext(worker_id="w1", task_id="t-unclaimed")
    res = executor.execute_source_sync(task, ctx, adapter=MagicMock())
    assert res is False


def test_executor_rejects_wrong_worker(executor_env):
    executor, task_repo, _, _, _, db = executor_env
    task = Task(id="t-wrong", contact_id="c-exec", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    task_repo.claim_task("t-wrong", "w1", "lock-w1")

    # w2 attempts to execute task claimed by w1
    ctx = ExecutionContext(worker_id="w2", task_id="t-wrong")
    claimed_task = task_repo.get_by_id("t-wrong")
    res = executor.execute_source_sync(claimed_task, ctx, adapter=MagicMock())
    assert res is False


def test_executor_successful_completion_and_lock_release(executor_env):
    executor, task_repo, event_repo, _, source_service, db = executor_env
    task = Task(id="t-ok", contact_id="c-exec", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    task_repo.claim_task("t-ok", "w1", "lock-ok")

    claimed_task = task_repo.get_by_id("t-ok")
    ctx = ExecutionContext(worker_id="w1", task_id="t-ok")
    res = executor.execute_source_sync(claimed_task, ctx, adapter=MagicMock())

    assert res is True
    t = task_repo.get_by_id("t-ok")
    assert t.status == TaskState.COMPLETED
    assert t.lock_token is None


def test_executor_browser_crash_marks_interrupted(executor_env):
    executor, task_repo, _, error_repo, source_service, db = executor_env
    source_service.sync_source.side_effect = BrowserCrashError("Browser disconnected")

    task = Task(id="t-crash", contact_id="c-exec", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    task_repo.claim_task("t-crash", "w1", "lock-crash")

    claimed_task = task_repo.get_by_id("t-crash")
    ctx = ExecutionContext(worker_id="w1", task_id="t-crash")
    res = executor.execute_source_sync(claimed_task, ctx, adapter=MagicMock())

    assert res is False
    t = task_repo.get_by_id("t-crash")
    assert t.status == TaskState.INTERRUPTED
    assert t.lock_token is None


def test_executor_timeout_marks_retry_wait(executor_env):
    executor, task_repo, _, _, source_service, db = executor_env
    source_service.sync_source.side_effect = BrowserTimeoutError("Operation timed out")

    task = Task(id="t-timeout", contact_id="c-exec", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    task_repo.claim_task("t-timeout", "w1", "lock-timeout")

    claimed_task = task_repo.get_by_id("t-timeout")
    ctx = ExecutionContext(worker_id="w1", task_id="t-timeout")
    res = executor.execute_source_sync(claimed_task, ctx, adapter=MagicMock())

    assert res is False
    t = task_repo.get_by_id("t-timeout")
    assert t.status == TaskState.RETRY_WAIT
    assert t.lock_token is None


def test_executor_conflict_marks_manual_review(executor_env):
    executor, task_repo, _, _, source_service, db = executor_env
    source_service.sync_source.side_effect = ConflictError("Spreadsheet modified concurrently")

    task = Task(id="t-conf", contact_id="c-exec", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    task_repo.claim_task("t-conf", "w1", "lock-conf")

    claimed_task = task_repo.get_by_id("t-conf")
    ctx = ExecutionContext(worker_id="w1", task_id="t-conf")
    res = executor.execute_source_sync(claimed_task, ctx, adapter=MagicMock())

    assert res is False
    t = task_repo.get_by_id("t-conf")
    assert t.status == TaskState.MANUAL_REVIEW
    assert t.lock_token is None
