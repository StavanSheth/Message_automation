"""Deterministic End-to-End Integration Test for Phase 2.

Tests the complete flow:
Spreadsheet Source (Browser & XLSX)
      ↓
Source Adapter
      ↓
Source Service
      ↓
SQLite Database (Contacts, SourceRecords, Messages, Tasks, Followups)
      ↓
Worker Claiming (Atomicity & Lock Token)
      ↓
Browser Session Instance
      ↓
Task Executor (ExecutionContext Propagation)
      ↓
Completion (State Transitions, Lock Release)
      ↓
Events / SyncRun / Error Handling
"""

import os
import json
import pytest

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
from backend.browser.manager import BrowserManager
from backend.workers.default_manager import DefaultWorkerManager
from backend.workers.worker import Worker
from backend.domain.models import Task, Contact
from backend.domain.enums import (
    WorkerMode,
    WorkerStatus,
    TaskState,
    TaskType,
    SyncStatus,
    EventCode,
    SourceAccessStatus,
)
from backend.config.settings import AppSettings, reset_settings, set_settings
from backend.events.correlation import reset_counters, generate_id
from tests.fixtures.spreadsheet_page import (
    DeterministicSpreadsheetPage,
    create_deterministic_session,
)
from backend.sources.browser_sheet.adapter import BrowserSpreadsheetSource
from backend.sources.browser_sheet.driver import PlaywrightSpreadsheetDriver
from backend.sources.xlsx.adapter import LocalXlsxSource


@pytest.fixture(autouse=True)
def clean_env():
    reset_settings()
    reset_counters()
    yield
    reset_settings()
    reset_counters()


@pytest.fixture
def e2e_env(tmp_path):
    db_path = str(tmp_path / "e2e_full.db")
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

    # Deterministic spreadsheet page with 2 contacts
    page = DeterministicSpreadsheetPage()
    session = create_deterministic_session(page=page, session_id="SESS-E2E-1", worker_id="WKR-E2E-1")
    driver = PlaywrightSpreadsheetDriver(session=session)
    source = BrowserSpreadsheetSource(
        spreadsheet_url="https://docs.google.com/spreadsheets/d/test_e2e_sheet/edit",
        driver=driver,
    )

    worker_manager = DefaultWorkerManager(
        task_repo=task_repo,
        event_repo=evt_repo,
        task_executor=executor,
    )

    yield {
        "db": db,
        "contact_repo": contact_repo,
        "srec_repo": srec_repo,
        "task_repo": task_repo,
        "msg_repo": msg_repo,
        "fu_repo": fu_repo,
        "evt_repo": evt_repo,
        "sync_repo": sync_repo,
        "err_repo": err_repo,
        "source_service": source_service,
        "executor": executor,
        "source": source,
        "session": session,
        "page": page,
        "worker_manager": worker_manager,
    }

    worker_manager.shutdown_all()
    session.stop()


def test_browser_spreadsheet_end_to_end_pipeline(e2e_env):
    """
    Complete end-to-end verification:
    1. Browser spreadsheet synchronization creates contacts, tasks, messages, followups, sync run, events.
    2. Worker starts, claims task atomically, and executes via TaskExecutor with ExecutionContext.
    3. Task completes cleanly, lock is released, worker returns to IDLE.
    4. Write-back updates spreadsheet and is verified by read-back.
    """
    source_service = e2e_env["source_service"]
    source = e2e_env["source"]
    contact_repo = e2e_env["contact_repo"]
    srec_repo = e2e_env["srec_repo"]
    task_repo = e2e_env["task_repo"]
    msg_repo = e2e_env["msg_repo"]
    fu_repo = e2e_env["fu_repo"]
    evt_repo = e2e_env["evt_repo"]
    sync_repo = e2e_env["sync_repo"]
    executor = e2e_env["executor"]
    session = e2e_env["session"]

    # ── Step 1: Ingest Spreadsheet ──
    sync_run = source_service.sync_source(source)
    assert sync_run.status == SyncStatus.SUCCESS
    assert sync_run.records_read == 2
    assert sync_run.records_written == 2
    assert sync_run.conflicts == 0

    # Verify Contacts
    alice = contact_repo.get_by_instagram_url("https://instagram.com/alice_j")
    bob = contact_repo.get_by_instagram_url("https://instagram.com/bob_smith")
    assert alice is not None and alice.name == "Alice Johnson"
    assert bob is not None and bob.name == "Bob Smith"

    # Verify SourceRecords
    srecs = srec_repo.list_by_source(source.source_identifier)
    assert len(srecs) == 2

    # Verify Task & Message (Alice has not replied, so Task created)
    alice_task = task_repo.get_by_contact_and_type(alice.id, TaskType.MESSAGE)
    assert alice_task is not None
    assert alice_task.status == TaskState.READY
    assert alice_task.lock_token is None

    alice_msg = msg_repo.get_by_task_id(alice_task.id)
    assert alice_msg is not None
    assert alice_msg.body == "Hi Alice, let's connect!"

    # Verify Followups
    followups = fu_repo.list_by_contact(alice.id)
    assert len(followups) == 2

    # ── Step 2: Worker Execution Pipeline ──
    worker = Worker(
        worker_id="WKR-E2E-1",
        worker_code="worker-e2e-1",
        mode=WorkerMode.SINGLE_BROWSER,
        task_repo=task_repo,
        event_repo=evt_repo,
        session=session,
    )
    worker.start()
    assert worker.status == WorkerStatus.IDLE

    # Worker claims task
    claimed = worker.claim_task(alice_task.id)
    assert claimed is True
    assert worker.status == WorkerStatus.BUSY

    t_running = task_repo.get_by_id(alice_task.id)
    assert t_running.status == TaskState.RUNNING
    assert t_running.worker_id == "WKR-E2E-1"
    assert t_running.lock_token is not None

    # Worker executes task
    ctx = ExecutionContext(
        task_id=alice_task.id,
        worker_id=worker.worker_id,
        run_id=generate_id("RUN"),
        session_id=session.session_id,
        correlation_id=generate_id("CORR"),
    )
    res = executor.execute_source_sync(t_running, ctx, adapter=source, session=session)
    assert res is True

    # ── Step 3: Verify Completion and Lock Release ──
    t_completed = task_repo.get_by_id(alice_task.id)
    assert t_completed.status == TaskState.COMPLETED
    assert t_completed.lock_token is None

    worker.release_current_task()
    assert worker.status == WorkerStatus.IDLE
    assert worker.current_task_id is None

    # Verify events
    events = evt_repo.list_events(limit=50)
    event_codes = [e.event_code for e in events]
    assert EventCode.TASK_EXECUTION_STARTED in event_codes
    assert EventCode.TASK_EXECUTION_COMPLETED in event_codes

    # ── Step 4: Write-Back and Read-Back Verification ──
    updated = source.update_record("2", {"replied": "YES"})
    assert updated is True
    read_back = source.driver.read_cell(source.url, 2, "replied")
    assert read_back == "YES"

    worker.stop()
    assert worker.status == WorkerStatus.STOPPED


def test_xlsx_preservation_end_to_end(e2e_env):
    """Verify Phase 1 XLSX source ingestion and execution is preserved."""
    fixtures_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fixtures")
    xlsx_path = os.path.join(fixtures_dir, "sample_contacts.xlsx")
    if not os.path.exists(xlsx_path):
        pytest.skip("sample_contacts.xlsx not found")

    source_service = e2e_env["source_service"]
    contact_repo = e2e_env["contact_repo"]
    task_repo = e2e_env["task_repo"]
    executor = e2e_env["executor"]

    adapter = LocalXlsxSource(xlsx_path)
    sync_run = source_service.sync_source(adapter)
    assert sync_run.status == SyncStatus.SUCCESS
    assert sync_run.records_read > 0

    # Ensure ready task can be claimed and executed
    tasks = task_repo.list_ready(limit=1)
    if tasks:
        t = tasks[0]
        claimed = task_repo.claim_task(t.id, "WKR-XLSX", "lock-xlsx")
        assert claimed is True

        ctx = ExecutionContext(
            task_id=t.id,
            worker_id="WKR-XLSX",
            run_id="run-xlsx",
            correlation_id=generate_id("CORR"),
        )
        t_claimed = task_repo.get_by_id(t.id)
        res = executor.execute_source_sync(t_claimed, ctx, adapter=adapter)
        assert res is True

        t_done = task_repo.get_by_id(t.id)
        assert t_done.status == TaskState.COMPLETED
        assert t_done.lock_token is None
