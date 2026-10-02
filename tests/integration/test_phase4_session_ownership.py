"""Integration tests for Browser / Session Ownership invariants (Section 7)."""

import pytest
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.task_repo import TaskRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.account_repo import AccountRepository
from backend.repositories.worker_repo import WorkerRepository
from backend.repositories.browser_session_repo import BrowserSessionRepository
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.event_repo import EventRepository
from backend.application.execution_service import ExecutionService
from backend.domain.models import Task, Account, WorkerRecord, Contact, BrowserSession
from backend.domain.enums import TaskState, TaskType, WorkerMode, WorkerStatus, EventCode
from tests.fixtures.mock_browser import create_mock_session


@pytest.fixture
def session_env(tmp_path):
    db_path = str(tmp_path / "test_session_own.db")
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()

    task_repo = TaskRepository(db)
    msg_repo = MessageRepository(db)
    account_repo = AccountRepository(db)
    worker_repo = WorkerRepository(db)
    session_repo = BrowserSessionRepository(db)
    contact_repo = ContactRepository(db)
    event_repo = EventRepository(db)

    # Seed contact and account
    contact_repo.create(Contact(id="cnt-sess-1", name="Sess Contact", instagram_url="https://instagram.com/sess_user"))
    account_repo.create(
        Account(
            id="acc-sess-1",
            username="sess_user",
            status="ACTIVE",
            profile_path="/data/profiles/sess_user",
        )
    )

    worker_repo.create(
        WorkerRecord(
            id="wkr-sess-1",
            worker_code="W-SESS",
            mode=WorkerMode.SINGLE_BROWSER,
            status=WorkerStatus.IDLE,
            account_id="acc-sess-1",
        )
    )

    auto_svc = type("AutoMock", (), {"execute_messaging_task": lambda *a, **k: True})()

    exec_svc = ExecutionService(
        task_repo=task_repo,
        message_repo=msg_repo,
        automation_service=auto_svc,
        worker_repo=worker_repo,
        account_repo=account_repo,
        event_repo=event_repo,
    )

    return {
        "db": db,
        "task_repo": task_repo,
        "account_repo": account_repo,
        "worker_repo": worker_repo,
        "session_repo": session_repo,
        "exec_svc": exec_svc,
    }


def test_session_ownership_match_succeeds(session_env):
    """When Session.worker_id == Worker.id, Session.account_id == Account.id, execution proceeds."""
    task_repo = session_env["task_repo"]
    account_repo = session_env["account_repo"]
    exec_svc = session_env["exec_svc"]

    session = create_mock_session("SESS-OK")
    session.worker_id = "wkr-sess-1"
    session.account_id = "acc-sess-1"
    session.profile_path = "/data/profiles/sess_user"
    session.auth_status = "AUTHENTICATED"

    account_repo.assign_worker("acc-sess-1", "wkr-sess-1", session.session_id)

    task = Task(id="t-sess-ok", contact_id="cnt-sess-1", type=TaskType.MESSAGE, status=TaskState.READY, account_id="acc-sess-1")
    task_repo.create(task)
    lease_id = task_repo.acquire_lease("t-sess-ok", "wkr-sess-1")

    success = exec_svc.execute_task(task_id="t-sess-ok", session=session, worker_id="wkr-sess-1", lease_id=lease_id)
    assert success is True


def test_session_ownership_worker_mismatch_quarantines(session_env):
    """Session owned by a different worker must be rejected and worker quarantined."""
    task_repo = session_env["task_repo"]
    worker_repo = session_env["worker_repo"]
    exec_svc = session_env["exec_svc"]

    session = create_mock_session("SESS-DIFF-WKR")
    session.worker_id = "wkr-other-worker"
    session.account_id = "acc-sess-1"
    session.profile_path = "/data/profiles/sess_user"
    session.auth_status = "AUTHENTICATED"

    task = Task(id="t-sess-wkr-mismatch", contact_id="cnt-sess-1", type=TaskType.MESSAGE, status=TaskState.READY, account_id="acc-sess-1")
    task_repo.create(task)
    lease_id = task_repo.acquire_lease("t-sess-wkr-mismatch", "wkr-sess-1")

    success = exec_svc.execute_task(task_id="t-sess-wkr-mismatch", session=session, worker_id="wkr-sess-1", lease_id=lease_id)
    assert success is False

    wkr = worker_repo.get_by_id("wkr-sess-1")
    assert wkr.status == WorkerStatus.QUARANTINED
    assert "ownership_mismatch" in str(wkr.quarantine_reason)


def test_session_ownership_profile_mismatch_rejected(session_env):
    """Session with different profile_path than account must be rejected."""
    task_repo = session_env["task_repo"]
    account_repo = session_env["account_repo"]
    exec_svc = session_env["exec_svc"]

    session = create_mock_session("SESS-DIFF-PROF")
    session.worker_id = "wkr-sess-1"
    session.account_id = "acc-sess-1"
    session.profile_path = "/data/profiles/wrong_profile"
    session.auth_status = "AUTHENTICATED"

    account_repo.assign_worker("acc-sess-1", "wkr-sess-1", session.session_id)

    task = Task(id="t-sess-prof-mismatch", contact_id="cnt-sess-1", type=TaskType.MESSAGE, status=TaskState.READY, account_id="acc-sess-1")
    task_repo.create(task)
    lease_id = task_repo.acquire_lease("t-sess-prof-mismatch", "wkr-sess-1")

    success = exec_svc.execute_task(task_id="t-sess-prof-mismatch", session=session, worker_id="wkr-sess-1", lease_id=lease_id)
    assert success is False
