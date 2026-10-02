"""Integration tests for Authentication Lifecycle integration (Section 8)."""

import pytest
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.task_repo import TaskRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.account_repo import AccountRepository
from backend.repositories.worker_repo import WorkerRepository
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.event_repo import EventRepository
from backend.application.execution_service import ExecutionService
from backend.scheduler.task_dispatcher import TaskDispatcher
from backend.domain.models import Task, Account, WorkerRecord, Contact
from backend.domain.enums import TaskState, TaskType, WorkerMode, WorkerStatus, SessionAuthState
from tests.fixtures.mock_browser import create_mock_session


@pytest.fixture
def auth_env(tmp_path):
    db_path = str(tmp_path / "test_auth_life.db")
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()

    task_repo = TaskRepository(db)
    msg_repo = MessageRepository(db)
    account_repo = AccountRepository(db)
    worker_repo = WorkerRepository(db)
    contact_repo = ContactRepository(db)
    event_repo = EventRepository(db)

    contact_repo.create(Contact(id="cnt-auth-1", name="Auth Contact", instagram_url="https://instagram.com/auth_user"))
    account_repo.create(Account(id="acc-auth-1", username="auth_user", status="ACTIVE"))

    worker_repo.create(
        WorkerRecord(
            id="wkr-auth-1",
            worker_code="W-AUTH",
            mode=WorkerMode.SINGLE_BROWSER,
            status=WorkerStatus.IDLE,
            account_id="acc-auth-1",
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

    dispatcher = TaskDispatcher(
        task_repo=task_repo,
        account_repo=account_repo,
    )

    return {
        "db": db,
        "task_repo": task_repo,
        "account_repo": account_repo,
        "worker_repo": worker_repo,
        "exec_svc": exec_svc,
        "dispatcher": dispatcher,
    }


def test_auth_lifecycle_scheduler_gates_unauthenticated_session(auth_env):
    """Scheduler TaskDispatcher must reject task assignment when worker session is not AUTHENTICATED."""
    task_repo = auth_env["task_repo"]
    dispatcher = auth_env["dispatcher"]

    task = Task(id="t-disp-auth", contact_id="cnt-auth-1", type=TaskType.MESSAGE, status=TaskState.READY, account_id="acc-auth-1")
    task_repo.create(task)

    # Worker with UNKNOWN auth session
    class WorkerMock:
        worker_id = "wkr-auth-1"
        status = WorkerStatus.IDLE
        account_id = "acc-auth-1"
        session = create_mock_session("SESS-UNK")

    WorkerMock.session.auth_status = "UNKNOWN"
    WorkerMock.session.account_id = "acc-auth-1"

    eligible, reason = dispatcher.is_task_eligible(task, worker=WorkerMock)
    assert eligible is False
    assert "auth" in reason.lower()


def test_auth_lifecycle_challenge_quarantines_worker(auth_env):
    """CHALLENGE or CHECKPOINT auth status must quarantine worker and escalate task to MANUAL_REVIEW."""
    task_repo = auth_env["task_repo"]
    worker_repo = auth_env["worker_repo"]
    account_repo = auth_env["account_repo"]
    exec_svc = auth_env["exec_svc"]

    session = create_mock_session("SESS-CHALLENGE")
    session.worker_id = "wkr-auth-1"
    session.account_id = "acc-auth-1"
    session.auth_status = "CHALLENGE"

    account_repo.assign_worker("acc-auth-1", "wkr-auth-1", session.session_id)

    task = Task(id="t-chall", contact_id="cnt-auth-1", type=TaskType.MESSAGE, status=TaskState.READY, account_id="acc-auth-1")
    task_repo.create(task)
    lease_id = task_repo.acquire_lease("t-chall", "wkr-auth-1")

    success = exec_svc.execute_task(task_id="t-chall", session=session, worker_id="wkr-auth-1", lease_id=lease_id)
    assert success is False

    # Check worker quarantine
    wkr = worker_repo.get_by_id("wkr-auth-1")
    assert wkr.status == WorkerStatus.QUARANTINED
    assert "challenge" in str(wkr.quarantine_reason).lower()

    # Check task moved to MANUAL_REVIEW
    t = task_repo.get_by_id("t-chall")
    assert t.status == TaskState.MANUAL_REVIEW
