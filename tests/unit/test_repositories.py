"""Unit tests for newly added dedicated repositories:
- AutomationRunRepository
- WorkerRepository
- BrowserSessionRepository
- VerificationResultRepository
"""

import pytest
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.automation_run_repo import AutomationRunRepository
from backend.repositories.worker_repo import WorkerRepository
from backend.repositories.browser_session_repo import BrowserSessionRepository
from backend.repositories.verification_result_repo import VerificationResultRepository
from backend.repositories.contact_repo import ContactRepository
from backend.domain.models import AutomationRun, WorkerRecord, BrowserSession, VerificationResult, Contact
from backend.domain.enums import WorkerMode, WorkerStatus, VerificationDecision


@pytest.fixture
def db(tmp_path):
    mgr = DatabaseManager(str(tmp_path / "test_repos.db"))
    MigrationRunner(mgr).apply_pending()
    return mgr


def test_automation_run_repo(db):
    repo = AutomationRunRepository(db)
    run = AutomationRun(
        id="RUN-1",
        run_code="CODE-1",
        status="RUNNING",
        started_at="2026-10-01T10:00:00Z",
        worker_id="W-1",
        task_id="T-1",
    )
    repo.create(run)

    fetched = repo.get_by_id("RUN-1")
    assert fetched is not None
    assert fetched.run_code == "CODE-1"
    assert fetched.status == "RUNNING"

    fetched_code = repo.get_by_run_code("CODE-1")
    assert fetched_code is not None
    assert fetched_code.id == "RUN-1"

    run.status = "COMPLETED"
    run.ended_at = "2026-10-01T10:05:00Z"
    repo.update(run)

    updated = repo.get_by_id("RUN-1")
    assert updated.status == "COMPLETED"
    assert updated.ended_at == "2026-10-01T10:05:00Z"

    runs = repo.list_runs(status="COMPLETED")
    assert len(runs) == 1
    assert runs[0].id == "RUN-1"


def test_worker_repo(db):
    repo = WorkerRepository(db)
    worker = WorkerRecord(
        id="WRK-1",
        worker_code="WCODE-1",
        mode=WorkerMode.SINGLE_BROWSER,
        status=WorkerStatus.IDLE,
        last_heartbeat="2026-10-01T10:00:00Z",
    )
    repo.create(worker)

    fetched = repo.get_by_id("WRK-1")
    assert fetched is not None
    assert fetched.worker_code == "WCODE-1"
    assert fetched.mode == WorkerMode.SINGLE_BROWSER
    assert fetched.status == WorkerStatus.IDLE

    fetched_code = repo.get_by_code("WCODE-1")
    assert fetched_code is not None
    assert fetched_code.id == "WRK-1"

    worker.status = WorkerStatus.BUSY
    worker.current_task_id = "TASK-99"
    repo.update(worker)

    updated = repo.get_by_id("WRK-1")
    assert updated.status == WorkerStatus.BUSY
    assert updated.current_task_id == "TASK-99"

    busy_workers = repo.list_workers(status=WorkerStatus.BUSY)
    assert len(busy_workers) == 1

    deleted = repo.delete("WRK-1")
    assert deleted is True
    assert repo.get_by_id("WRK-1") is None


def test_browser_session_repo(db):
    repo = BrowserSessionRepository(db)
    session = BrowserSession(
        id="SESS-1",
        profile_path="/path/to/profile",
        status="ACTIVE",
        started_at="2026-10-01T10:00:00Z",
        worker_id="WRK-1",
    )
    repo.create(session)

    fetched = repo.get_by_id("SESS-1")
    assert fetched is not None
    assert fetched.profile_path == "/path/to/profile"
    assert fetched.status == "ACTIVE"
    assert fetched.closed_at is None

    by_worker = repo.get_by_worker_id("WRK-1")
    assert len(by_worker) == 1
    assert by_worker[0].id == "SESS-1"

    active = repo.list_active()
    assert len(active) == 1

    closed = repo.close_session("SESS-1", closed_at="2026-10-01T10:10:00Z")
    assert closed.status == "CLOSED"
    assert closed.closed_at == "2026-10-01T10:10:00Z"
    assert len(repo.list_active()) == 0


def test_verification_result_repo(db):
    contact_repo = ContactRepository(db)
    contact_repo.create(Contact(id="CNT-1", name="Alice", instagram_url="https://instagr.am/alice"))

    from backend.repositories.task_repo import TaskRepository
    from backend.domain.models import Task
    from backend.domain.enums import TaskType, TaskState
    task_repo = TaskRepository(db)
    task_repo.create(
        Task(
            id="TSK-1",
            contact_id="CNT-1",
            type=TaskType.MESSAGE,
            sequence=0,
            status=TaskState.READY,
        )
    )

    repo = VerificationResultRepository(db)
    result = VerificationResult(
        id="VR-1",
        contact_id="CNT-1",
        task_id="TSK-1",
        confidence=0.95,
        decision=VerificationDecision.HIGH_CONFIDENCE,
        signals_json='{"name_match": true}',
        ocr_text="Alice Profile",
        created_at="2026-10-01T10:00:00Z",
    )
    repo.create(result)

    fetched = repo.get_by_id("VR-1")
    assert fetched is not None
    assert fetched.confidence == 0.95
    assert fetched.decision == VerificationDecision.HIGH_CONFIDENCE
    assert fetched.signals_json == '{"name_match": true}'
    assert fetched.ocr_text == "Alice Profile"

    by_task = repo.get_by_task_id("TSK-1")
    assert by_task is not None
    assert by_task.id == "VR-1"

    by_contact = repo.list_by_contact_id("CNT-1")
    assert len(by_contact) == 1
    assert by_contact[0].id == "VR-1"
