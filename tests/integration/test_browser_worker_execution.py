"""Integration test: Worker lifecycle, task claiming, ExecutionContext propagation, and TaskExecutor.

Tests the full worker execution pipeline:
- Worker start: IDLE, heartbeat active, session alive
- Worker claim: RUNNING, worker_id, lock_token, BUSY
- ExecutionContext propagation: run_id, task_id, worker_id, session_id, correlation_id
- Successful completion: COMPLETED, lock released, worker IDLE, session healthy
- Failed execution: ErrorRecord created with correct ErrorCode, lock released, worker IDLE
- Ownership loss / lock stealing guard: executor does not complete task if ownership changed
"""

import pytest
from unittest.mock import MagicMock

from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.task_repo import TaskRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.error_repo import ErrorRepository
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.source_record_repo import SourceRecordRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.followup_repo import FollowupRepository
from backend.repositories.sync_run_repo import SyncRunRepository
from backend.application.source_service import SourceService
from backend.automation.task_executor import TaskExecutor
from backend.automation.execution_context import ExecutionContext
from backend.workers.worker import Worker
from backend.domain.models import Task, Contact, SourceRow
from backend.domain.enums import (
    WorkerMode,
    WorkerStatus,
    TaskState,
    TaskType,
    ErrorCode,
    EventCode,
    RepliedStatus,
    SourceAccessStatus,
)
from backend.domain.errors import SourceAccessError
from backend.config.settings import reset_settings
from backend.events.correlation import reset_counters, generate_id
from tests.fixtures.spreadsheet_page import (
    DeterministicSpreadsheetPage,
    create_deterministic_session,
)
from backend.sources.browser_sheet.adapter import BrowserSpreadsheetSource
from backend.sources.browser_sheet.driver import PlaywrightSpreadsheetDriver


@pytest.fixture(autouse=True)
def clean_env():
    reset_settings()
    reset_counters()
    yield
    reset_settings()
    reset_counters()


@pytest.fixture
def worker_exec_env(tmp_path):
    db_path = str(tmp_path / "worker_exec_test.db")
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()

    contact_repo = ContactRepository(db)
    srec_repo = SourceRecordRepository(db)
    task_repo = TaskRepository(db)
    msg_repo = MessageRepository(db)
    fu_repo = FollowupRepository(db)
    evt_repo = EventRepository(db)
    sync_repo = SyncRunRepository(db)
    err_repo = ErrorRepository(db)

    # Pre-create contact for FK
    contact_repo.create(Contact(id="c-wkr-test", name="Worker Test Contact", instagram_url="https://instagram.com/wkr_test"))

    source_service = SourceService(
        contact_repo=contact_repo,
        source_record_repo=srec_repo,
        task_repo=task_repo,
        message_repo=msg_repo,
        followup_repo=fu_repo,
        event_repo=evt_repo,
        sync_run_repo=sync_repo,
    )

    executor = TaskExecutor(
        task_repo=task_repo,
        event_repo=evt_repo,
        error_repo=err_repo,
        source_service=source_service,
    )

    page = DeterministicSpreadsheetPage()
    session = create_deterministic_session(page=page, session_id="SESS-WKR-1", worker_id="WKR-1")
    driver = PlaywrightSpreadsheetDriver(session=session)
    source = BrowserSpreadsheetSource(
        spreadsheet_url="https://docs.google.com/spreadsheets/d/test_wkr_sheet/edit",
        driver=driver,
    )

    worker = Worker(
        worker_id="WKR-1",
        worker_code="worker-1",
        mode=WorkerMode.SINGLE_BROWSER,
        task_repo=task_repo,
        event_repo=evt_repo,
        session=session,
        heartbeat_interval=0.1,
    )

    yield {
        "db": db,
        "worker": worker,
        "executor": executor,
        "source": source,
        "session": session,
        "page": page,
        "task_repo": task_repo,
        "evt_repo": evt_repo,
        "err_repo": err_repo,
        "contact_repo": contact_repo,
    }

    worker.stop()
    session.stop()


def test_worker_startup_state(worker_exec_env):
    """1. Worker starts: WorkerStatus.IDLE, heartbeat active, browser session alive."""
    worker = worker_exec_env["worker"]
    session = worker_exec_env["session"]

    worker.start()
    assert worker.status == WorkerStatus.IDLE
    assert worker.last_heartbeat is not None
    assert session.is_alive()


def test_worker_claims_task(worker_exec_env):
    """2. Worker claims task: RUNNING, worker_id, lock_token exists, worker BUSY."""
    worker = worker_exec_env["worker"]
    task_repo = worker_exec_env["task_repo"]

    task = Task(id="t-claim-test", contact_id="c-wkr-test", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)

    worker.start()
    claimed = worker.claim_task("t-claim-test")
    assert claimed is True
    assert worker.status == WorkerStatus.BUSY
    assert worker.current_task_id == "t-claim-test"

    t = task_repo.get_by_id("t-claim-test")
    assert t.status == TaskState.RUNNING
    assert t.worker_id == "WKR-1"
    assert t.lock_token is not None
    assert t.attempt_count == 1


def test_execution_context_propagation(worker_exec_env):
    """3. ExecutionContext: run_id, task_id, worker_id, session_id, correlation_id propagated to events."""
    worker = worker_exec_env["worker"]
    executor = worker_exec_env["executor"]
    source = worker_exec_env["source"]
    task_repo = worker_exec_env["task_repo"]
    evt_repo = worker_exec_env["evt_repo"]

    task = Task(id="t-ctx-test", contact_id="c-wkr-test", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)

    worker.start()
    success = worker.process_next_task(executor=executor, adapter=source, assigned_task=task)
    assert success is True

    # Check TASK_EXECUTION_STARTED event payload
    import json
    events = evt_repo.list_events(limit=50)
    started_events = [e for e in events if e.event_code == EventCode.TASK_EXECUTION_STARTED]
    assert len(started_events) >= 1

    payload = json.loads(started_events[0].payload_json or "{}")
    assert payload["worker_id"] == "WKR-1"
    assert payload["session_id"] == "SESS-WKR-1"
    assert "run_id" in payload
    assert "correlation_id" in payload


def test_successful_execution_completion(worker_exec_env):
    """4. Successful completion: COMPLETED, lock_token released, worker IDLE, session healthy."""
    worker = worker_exec_env["worker"]
    executor = worker_exec_env["executor"]
    source = worker_exec_env["source"]
    task_repo = worker_exec_env["task_repo"]
    session = worker_exec_env["session"]

    task = Task(id="t-comp-test", contact_id="c-wkr-test", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)

    worker.start()
    success = worker.process_next_task(executor=executor, adapter=source, assigned_task=task)
    assert success is True

    completed_task = task_repo.get_by_id("t-comp-test")
    assert completed_task.status == TaskState.COMPLETED
    assert completed_task.lock_token is None

    assert worker.status == WorkerStatus.IDLE
    assert worker.current_task_id is None
    assert session.is_alive()


def test_failed_execution_handling(worker_exec_env):
    """5. Failed execution: ErrorRecord created, correct ErrorCode, task RETRY_WAIT / FAILED, lock released."""
    worker = worker_exec_env["worker"]
    executor = worker_exec_env["executor"]
    task_repo = worker_exec_env["task_repo"]
    err_repo = worker_exec_env["err_repo"]

    # Real BrowserSpreadsheetSource configured with SOURCE_UNAVAILABLE
    failing_page = DeterministicSpreadsheetPage(access_state=SourceAccessStatus.SOURCE_UNAVAILABLE)
    failing_session = create_deterministic_session(page=failing_page, session_id="SESS-FAIL-1")
    failing_driver = PlaywrightSpreadsheetDriver(session=failing_session)
    failing_source = BrowserSpreadsheetSource(
        "https://docs.google.com/spreadsheets/d/test_fail/edit",
        driver=failing_driver,
    )

    task = Task(id="t-fail-test", contact_id="c-wkr-test", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)

    worker.start()
    success = worker.process_next_task(executor=executor, adapter=failing_source, assigned_task=task)
    assert success is False

    t = task_repo.get_by_id("t-fail-test")
    assert t.status in (TaskState.RETRY_WAIT, TaskState.FAILED, TaskState.MANUAL_REVIEW, TaskState.INTERRUPTED)
    assert t.lock_token is None

    assert worker.status == WorkerStatus.IDLE
    assert worker.current_task_id is None

    # ErrorRecord created in repository
    errors = err_repo.list_by_task("t-fail-test")
    assert len(errors) >= 1
    assert errors[0].code in (ErrorCode.SOURCE_UNAVAILABLE, ErrorCode.BROWSER_CRASH)


def test_ownership_loss_guard(worker_exec_env):
    """6. Simulate task ownership changing during execution: executor does NOT complete task."""
    task_repo = worker_exec_env["task_repo"]
    executor = worker_exec_env["executor"]
    source = worker_exec_env["source"]

    task = Task(id="t-owner-test", contact_id="c-wkr-test", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)

    # Worker 1 claims task
    task_repo.claim_task("t-owner-test", "WKR-1", "lock-wkr-1")

    # Construct execution context for Worker 1
    ctx = ExecutionContext(
        task_id="t-owner-test",
        worker_id="WKR-1",
        run_id="run-owner-test",
        session_id="SESS-WKR-1",
        correlation_id=generate_id("CORR"),
    )

    # Simulate another recovery worker stealing / interrupting the task during sync
    original_sync = executor.source_service.sync_source
    def steal_during_sync(adapter):
        # Stolen or interrupted by crash recovery
        task_repo.release_task("t-owner-test", "lock-wkr-1")
        task_repo.update_state("t-owner-test", TaskState.INTERRUPTED, enforce_transition=False)
        return original_sync(adapter)

    executor.source_service.sync_source = steal_during_sync

    t_current = task_repo.get_by_id("t-owner-test")
    res = executor.execute_source_sync(t_current, ctx, adapter=source)
    assert res is False

    # The task must NOT be COMPLETED
    final_task = task_repo.get_by_id("t-owner-test")
    assert final_task.status != TaskState.COMPLETED
    assert final_task.status == TaskState.INTERRUPTED
