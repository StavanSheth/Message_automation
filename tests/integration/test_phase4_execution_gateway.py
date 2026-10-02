"""Integration tests proving ExecutionService is the strictly enforced message execution gateway."""

import pytest
from unittest.mock import MagicMock
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.task_repo import TaskRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.error_repo import ErrorRepository
from backend.repositories.account_repo import AccountRepository
from backend.repositories.worker_repo import WorkerRepository
from backend.repositories.contact_repo import ContactRepository
from backend.automation.task_executor import TaskExecutor
from backend.automation.execution_context import ExecutionContext
from backend.application.execution_service import ExecutionService
from backend.workers.worker import Worker
from backend.domain.models import Task, Message, Contact, Account
from backend.domain.enums import TaskState, TaskType, WorkerMode


class FakeSession:
    def __init__(self, session_id: str = "sess-gw-1", account_id: str = "acc-gw-1"):
        self.session_id = session_id
        self.account_id = account_id
        self.auth_status = "AUTHENTICATED"

    def is_alive(self) -> bool:
        return True


@pytest.fixture
def gateway_env(tmp_path):
    db_path = str(tmp_path / "test_gateway.db")
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()

    task_repo = TaskRepository(db)
    message_repo = MessageRepository(db)
    event_repo = EventRepository(db)
    error_repo = ErrorRepository(db)
    account_repo = AccountRepository(db)
    worker_repo = WorkerRepository(db)
    contact_repo = ContactRepository(db)

    account_repo.create(Account(id="acc-gw-1", username="gw_user", status="ACTIVE"))
    contact_repo.create(Contact(id="cnt-gw-1", name="GW Contact", instagram_url="https://instagram.com/gw_user"))

    return {
        "db": db,
        "task_repo": task_repo,
        "message_repo": message_repo,
        "event_repo": event_repo,
        "error_repo": error_repo,
        "account_repo": account_repo,
        "worker_repo": worker_repo,
    }


def test_task_executor_rejects_messaging_without_execution_service(gateway_env):
    """TaskExecutor must reject message execution when ExecutionService is missing or bypassed."""
    task_repo = gateway_env["task_repo"]
    event_repo = gateway_env["event_repo"]
    error_repo = gateway_env["error_repo"]

    task = Task(id="t-no-gw", contact_id="cnt-gw-1", type=TaskType.MESSAGE, status=TaskState.READY, account_id="acc-gw-1")
    task_repo.create(task)
    task_repo.claim_task(task.id, worker_id="worker-1", lock_token="lock-1")

    mock_automation_service = MagicMock()
    mock_automation_service.execute_messaging_task.return_value = True

    executor = TaskExecutor(
        task_repo=task_repo,
        event_repo=event_repo,
        error_repo=error_repo,
        instagram_service=mock_automation_service,
        execution_service=None,  # Explicitly omitted to simulate gateway bypass
    )
    # Ensure execution_service is strictly None
    executor.execution_service = None

    context = ExecutionContext(task_id=task.id, worker_id="worker-1")
    session = FakeSession()

    success = executor.execute_task(task=task, context=context, session=session)
    assert success is False

    # Automation service must NEVER have been called directly
    mock_automation_service.execute_messaging_task.assert_not_called()


def test_worker_rejects_messaging_without_execution_service(gateway_env):
    """Worker must reject message task if no ExecutionService is provided or injected."""
    task_repo = gateway_env["task_repo"]
    event_repo = gateway_env["event_repo"]

    task = Task(id="t-wkr-nogw", contact_id="cnt-gw-1", type=TaskType.MESSAGE, status=TaskState.READY, account_id="acc-gw-1")
    task_repo.create(task)

    worker = Worker(
        worker_id="wkr-no-gw",
        worker_code="worker-nogw",
        mode=WorkerMode.SINGLE_BROWSER,
        task_repo=task_repo,
        event_repo=event_repo,
        execution_service=None,
    )
    worker.start()

    # Pass an executor with no ExecutionService and no execute_task
    bare_executor = MagicMock(spec=[])
    success = worker.process_next_task(executor=bare_executor, assigned_task=task)
    assert success is False
