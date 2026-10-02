"""Integration tests for Phase 4 strict Account -> Profile -> Session -> Worker ownership."""

import pytest
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.task_repo import TaskRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.account_repo import AccountRepository
from backend.repositories.worker_repo import WorkerRepository
from backend.application.account_service import AccountService
from backend.application.execution_service import ExecutionService
from backend.domain.models import Task, Account, WorkerRecord, Message, utc_now_iso
from backend.domain.enums import TaskState, TaskType, WorkerMode, WorkerStatus, AccountStatus


class FakeSession:
    def __init__(self, session_id: str, account_id: str = "acc-owner-1"):
        self.session_id = session_id
        self.account_id = account_id
        self.auth_status = "AUTHENTICATED"

    def is_alive(self) -> bool:
        return True


class FakeAutomationService:
    def __init__(self):
        self.executed_tasks = []

    def execute_messaging_task(self, task, session, worker_id, correlation_id):
        self.executed_tasks.append((task.id, session.session_id, worker_id))
        return True


@pytest.fixture
def ownership_env(tmp_path):
    db_path = str(tmp_path / "test_ownership.db")
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()

    task_repo = TaskRepository(db)
    msg_repo = MessageRepository(db)
    account_repo = AccountRepository(db)
    worker_repo = WorkerRepository(db)

    account_svc = AccountService(account_repo=account_repo, worker_repo=worker_repo)
    auto_svc = FakeAutomationService()

    exec_svc = ExecutionService(
        task_repo=task_repo,
        message_repo=msg_repo,
        automation_service=auto_svc,
        worker_repo=worker_repo,
        account_repo=account_repo,
    )

    from backend.repositories.contact_repo import ContactRepository
    from backend.domain.models import Contact
    ContactRepository(db).create(Contact(id="c-own-1", name="Owner Contact", instagram_url="https://instagram.com/own1"))

    return {
        "db": db,
        "task_repo": task_repo,
        "msg_repo": msg_repo,
        "account_repo": account_repo,
        "worker_repo": worker_repo,
        "account_svc": account_svc,
        "exec_svc": exec_svc,
        "auto_svc": auto_svc,
    }


def test_execution_blocked_on_account_mismatch(ownership_env):
    task_repo = ownership_env["task_repo"]
    worker_repo = ownership_env["worker_repo"]
    account_repo = ownership_env["account_repo"]
    exec_svc = ownership_env["exec_svc"]

    # Register two accounts
    account_repo.create(Account(id="acc-A", username="userA", status="ACTIVE"))
    account_repo.create(Account(id="acc-B", username="userB", status="ACTIVE"))

    # Worker assigned to Account A
    worker_repo.create(
        WorkerRecord(
            id="WKR-A",
            worker_code="W-A",
            mode=WorkerMode.SINGLE_BROWSER,
            status=WorkerStatus.BUSY,
            account_id="acc-A",
        )
    )

    # Task assigned to Account B
    task = Task(
        id="t-mismatch-1",
        contact_id="c-own-1",
        type=TaskType.MESSAGE,
        status=TaskState.READY,
        account_id="acc-B",
        lease_id="LEASE-OWN-1",
        lease_owner="WKR-A",
    )
    task_repo.create(task)

    session_a = FakeSession(session_id="SESS-A", account_id="acc-A")

    # Execution must be BLOCKED due to ownership mismatch
    success = exec_svc.execute_task(
        task_id="t-mismatch-1",
        session=session_a,
        worker_id="WKR-A",
        lease_id="LEASE-OWN-1",
    )
    assert success is False
    assert len(ownership_env["auto_svc"].executed_tasks) == 0


def test_execution_allowed_when_ownership_matches(ownership_env):
    task_repo = ownership_env["task_repo"]
    worker_repo = ownership_env["worker_repo"]
    account_repo = ownership_env["account_repo"]
    exec_svc = ownership_env["exec_svc"]

    account_repo.create(Account(id="acc-C", username="userC", status="ACTIVE"))
    worker_repo.create(
        WorkerRecord(
            id="WKR-C",
            worker_code="W-C",
            mode=WorkerMode.SINGLE_BROWSER,
            status=WorkerStatus.BUSY,
            account_id="acc-C",
        )
    )

    task = Task(
        id="t-match-1",
        contact_id="c-own-1",
        type=TaskType.MESSAGE,
        status=TaskState.READY,
        account_id="acc-C",
        lease_id="LEASE-OWN-C",
        lease_owner="WKR-C",
    )
    task_repo.create(task)

    session_c = FakeSession(session_id="SESS-C", account_id="acc-C")

    # Execution should succeed
    success = exec_svc.execute_task(
        task_id="t-match-1",
        session=session_c,
        worker_id="WKR-C",
        lease_id="LEASE-OWN-C",
    )
    assert success is True
    assert len(ownership_env["auto_svc"].executed_tasks) == 1
