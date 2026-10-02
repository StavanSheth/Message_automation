"""Unit tests for ExecutionService gateway enforcing safety invariants before any message dispatch."""

import pytest
from unittest.mock import MagicMock
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.task_repo import TaskRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.account_repo import AccountRepository
from backend.repositories.worker_repo import WorkerRepository
from backend.repositories.contact_repo import ContactRepository
from backend.application.execution_service import ExecutionService
from backend.domain.models import Task, Message, Contact, Account, WorkerRecord
from backend.domain.enums import TaskState, TaskType, WorkerStatus


class MockSession:
    def __init__(self, session_id: str = "sess-gate-1", account_id: str = "acc-gate-1", auth_status: str = "AUTHENTICATED"):
        self.session_id = session_id
        self.account_id = account_id
        self.auth_status = auth_status

    def is_alive(self) -> bool:
        return True


@pytest.fixture
def exec_env(tmp_path):
    db_path = str(tmp_path / "test_exec_gw.db")
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()

    task_repo = TaskRepository(db)
    message_repo = MessageRepository(db)
    event_repo = EventRepository(db)
    account_repo = AccountRepository(db)
    worker_repo = WorkerRepository(db)
    contact_repo = ContactRepository(db)

    # Seed account and contact
    account_repo.create(Account(id="acc-gate-1", username="gw_user", status="ACTIVE"))
    contact_repo.create(Contact(id="cnt-gate-1", name="Gate Contact", instagram_url="https://instagram.com/gw_user"))

    auto_svc = MagicMock()
    auto_svc.execute_messaging_task.return_value = True

    svc = ExecutionService(
        task_repo=task_repo,
        message_repo=message_repo,
        automation_service=auto_svc,
        event_repo=event_repo,
        account_repo=account_repo,
        worker_repo=worker_repo,
    )

    return {
        "db": db,
        "task_repo": task_repo,
        "message_repo": message_repo,
        "exec_svc": svc,
        "auto_svc": auto_svc,
    }


def test_execution_rejected_for_ineligible_task_state(exec_env):
    """Tasks in RECONCILING, MANUAL_REVIEW, CANCELLED, or COMPLETED state must be rejected."""
    task_repo = exec_env["task_repo"]
    exec_svc = exec_env["exec_svc"]
    session = MockSession()

    for i, invalid_state in enumerate((TaskState.RECONCILING, TaskState.MANUAL_REVIEW, TaskState.CANCELLED, TaskState.COMPLETED)):
        task = Task(id=f"t-state-{invalid_state.value}", contact_id="cnt-gate-1", sequence=i, type=TaskType.MESSAGE, status=invalid_state, account_id="acc-gate-1")
        task_repo.create(task)
        res = exec_svc.execute_task(task_id=task.id, session=session, worker_id="wkr-1", lease_id="L-1")
        assert res is False


def test_execution_rejected_for_invalid_lease(exec_env):
    """Tasks without an active, matching lease must be rejected immediately."""
    task_repo = exec_env["task_repo"]
    exec_svc = exec_env["exec_svc"]
    session = MockSession()

    task = Task(id="t-lease-test", contact_id="cnt-gate-1", sequence=99, type=TaskType.MESSAGE, status=TaskState.READY, account_id="acc-gate-1")
    task_repo.create(task)

    # Calling with invalid or missing lease
    res_none = exec_svc.execute_task(task_id=task.id, session=session, worker_id="wkr-1", lease_id=None)
    assert res_none is False

    res_invalid = exec_svc.execute_task(task_id=task.id, session=session, worker_id="wkr-1", lease_id="L-NONEXISTENT")
    assert res_invalid is False


def test_execution_rejected_for_invalid_auth_status(exec_env):
    """Sessions in LOGIN_REQUIRED, SESSION_EXPIRED, UNKNOWN, or CHALLENGE must be blocked."""
    task_repo = exec_env["task_repo"]
    exec_svc = exec_env["exec_svc"]

    for i, auth in enumerate(("LOGIN_REQUIRED", "SESSION_EXPIRED", "UNKNOWN", "CHALLENGE", "CHECKPOINT")):
        task_id = f"t-auth-{auth.lower()}"
        task = Task(id=task_id, contact_id="cnt-gate-1", sequence=10 + i, type=TaskType.MESSAGE, status=TaskState.READY, account_id="acc-gate-1")
        task_repo.create(task)
        lease_id = task_repo.acquire_lease(task_id=task_id, worker_id="wkr-1")

        session = MockSession(auth_status=auth)
        res = exec_svc.execute_task(task_id=task_id, session=session, worker_id="wkr-1", lease_id=lease_id)
        assert res is False


def test_execution_succeeds_when_all_invariants_met(exec_env):
    """When all ownership, lease, and authentication invariants are satisfied, execution succeeds."""
    task_repo = exec_env["task_repo"]
    message_repo = exec_env["message_repo"]
    exec_svc = exec_env["exec_svc"]
    auto_svc = exec_env["auto_svc"]

    task_id = "t-ok-1"
    task = Task(id=task_id, contact_id="cnt-gate-1", type=TaskType.MESSAGE, status=TaskState.READY, account_id="acc-gate-1")
    task_repo.create(task)
    message_repo.create(Message(id="m-ok-1", task_id=task_id, contact_id="cnt-gate-1", body="Approved text"))

    lease_id = task_repo.acquire_lease(task_id=task_id, worker_id="wkr-1")
    session = MockSession(auth_status="AUTHENTICATED")

    res = exec_svc.execute_task(task_id=task_id, session=session, worker_id="wkr-1", lease_id=lease_id)
    assert res is True
    auto_svc.execute_messaging_task.assert_called_once()
