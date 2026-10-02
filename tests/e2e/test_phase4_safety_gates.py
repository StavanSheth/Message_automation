"""E2E tests for Phase 4 critical safety gates and mandatory ExecutionService verification."""

import pytest
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.task_repo import TaskRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.account_repo import AccountRepository
from backend.repositories.worker_repo import WorkerRepository
from backend.repositories.contact_repo import ContactRepository
from backend.application.execution_service import ExecutionService
from backend.automation.task_executor import TaskExecutor
from backend.automation.execution_context import ExecutionContext
from backend.domain.models import Task, Message, Contact, WorkerRecord, Account
from backend.domain.enums import TaskState, TaskType, WorkerMode, WorkerStatus


class MockSession:
    def __init__(self, session_id: str = "sess-gate-1", auth_status: str = "AUTHENTICATED", account_id: str = "acc-gate-1"):
        self.session_id = session_id
        self.auth_status = auth_status
        self.account_id = account_id

    def is_alive(self) -> bool:
        return True


class MockAutomationService:
    def __init__(self):
        self.call_count = 0

    def execute_messaging_task(self, task, session, worker_id, correlation_id):
        self.call_count += 1
        return True


@pytest.fixture
def gate_env(tmp_path):
    db_path = str(tmp_path / "test_gates.db")
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()

    task_repo = TaskRepository(db)
    msg_repo = MessageRepository(db)
    account_repo = AccountRepository(db)
    worker_repo = WorkerRepository(db)
    contact_repo = ContactRepository(db)

    contact_repo.create(Contact(id="c-gate-1", name="Gate Contact", instagram_url="https://instagram.com/gate1"))
    account_repo.create(Account(id="acc-gate-1", username="gate_user", status="ACTIVE"))

    worker_repo.create(
        WorkerRecord(
            id="WKR-GATE-1",
            worker_code="W-GATE",
            mode=WorkerMode.SINGLE_BROWSER,
            status=WorkerStatus.BUSY,
            account_id="acc-gate-1",
            current_task_id="t-gate-1",
        )
    )

    auto_svc = MockAutomationService()
    exec_svc = ExecutionService(
        task_repo=task_repo,
        message_repo=msg_repo,
        automation_service=auto_svc,
        worker_repo=worker_repo,
        account_repo=account_repo,
    )

    return {
        "db": db,
        "task_repo": task_repo,
        "msg_repo": msg_repo,
        "account_repo": account_repo,
        "worker_repo": worker_repo,
        "auto_svc": auto_svc,
        "exec_svc": exec_svc,
    }


def test_safety_gate_unknown_auth_rejects_send(gate_env):
    task_repo = gate_env["task_repo"]
    exec_svc = gate_env["exec_svc"]
    auto_svc = gate_env["auto_svc"]

    task = Task(id="t-auth-1", contact_id="c-gate-1", type=TaskType.MESSAGE, status=TaskState.READY, account_id="acc-gate-1")
    task_repo.create(task)

    session = MockSession(auth_status="UNKNOWN")

    # UNKNOWN auth must fail closed
    success = exec_svc.execute_task(task_id="t-auth-1", session=session, worker_id="WKR-GATE-1", lease_id="L-1")
    assert success is False
    assert auto_svc.call_count == 0


def test_safety_gate_checkpoint_routes_to_manual_review(gate_env):
    task_repo = gate_env["task_repo"]
    exec_svc = gate_env["exec_svc"]
    auto_svc = gate_env["auto_svc"]

    task = Task(
        id="t-auth-2",
        contact_id="c-gate-1",
        type=TaskType.MESSAGE,
        status=TaskState.READY,
        account_id="acc-gate-1",
        lease_id="L-2",
        lease_owner="WKR-GATE-1",
        lock_token="L-2",
    )
    task_repo.create(task)

    session = MockSession(auth_status="CHECKPOINT")

    success = exec_svc.execute_task(task_id="t-auth-2", session=session, worker_id="WKR-GATE-1", lease_id="L-2")
    assert success is False
    assert auto_svc.call_count == 0

    t = task_repo.get_by_id("t-auth-2")
    assert t.status == TaskState.MANUAL_REVIEW


def test_safety_gate_task_executor_rejects_execution_service_bypass(gate_env):
    task_repo = gate_env["task_repo"]
    auto_svc = gate_env["auto_svc"]

    from backend.repositories.event_repo import EventRepository
    from backend.repositories.error_repo import ErrorRepository

    event_repo = EventRepository(gate_env["db"])
    error_repo = ErrorRepository(gate_env["db"])

    # TaskExecutor instantiated WITHOUT ExecutionService
    executor = TaskExecutor(
        task_repo=task_repo,
        event_repo=event_repo,
        error_repo=error_repo,
        instagram_service=auto_svc,
    )
    # Explicitly clear execution_service to test bypass rejection
    executor.execution_service = None

    task = Task(
        id="t-bypass-1",
        contact_id="c-gate-1",
        type=TaskType.MESSAGE,
        status=TaskState.RUNNING,
        lock_token="LOCK-1",
        worker_id="WKR-GATE-1",
        lease_id="LEASE-1",
    )
    task_repo.create(task)

    context = ExecutionContext(worker_id="WKR-GATE-1", task_id="t-bypass-1")
    session = MockSession()

    # Must be explicitly REJECTED because ExecutionService gateway is mandatory
    success = executor.execute_task(task=task, context=context, session=session)
    assert success is False
    assert auto_svc.call_count == 0
