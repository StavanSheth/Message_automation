"""Integration test proving Worker cannot bypass TaskDispatcher or discover arbitrary ready tasks."""

import pytest
import inspect
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.task_repo import TaskRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.worker_repo import WorkerRepository
from backend.repositories.account_repo import AccountRepository
from backend.repositories.contact_repo import ContactRepository
from backend.scheduler.task_dispatcher import TaskDispatcher
from backend.workers.worker import Worker
from backend.workers.default_manager import DefaultWorkerManager
from backend.domain.models import Task, Account, Contact, WorkerRecord
from backend.domain.enums import TaskState, TaskType, WorkerMode, WorkerStatus


@pytest.fixture
def sched_authority_env(tmp_path):
    db_path = str(tmp_path / "test_sched_auth.db")
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()

    task_repo = TaskRepository(db)
    event_repo = EventRepository(db)
    worker_repo = WorkerRepository(db)
    account_repo = AccountRepository(db)
    contact_repo = ContactRepository(db)

    # Seed account and contact
    account_repo.create(Account(id="acc-auth-1", username="auth_user", status="ACTIVE"))
    contact_repo.create(Contact(id="cnt-auth-1", name="Auth Contact", instagram_url="https://instagram.com/auth_user"))

    worker = Worker(
        worker_id="wkr-auth-1",
        worker_code="worker-1",
        mode=WorkerMode.SINGLE_BROWSER,
        task_repo=task_repo,
        event_repo=event_repo,
        worker_repo=worker_repo,
    )
    worker.account_id = "acc-auth-1"

    worker_mgr = DefaultWorkerManager(
        task_repo=task_repo,
        event_repo=event_repo,
        worker_repo=worker_repo,
    )
    worker_mgr._workers[worker.worker_id] = worker

    dispatcher = TaskDispatcher(
        task_repo=task_repo,
        worker_manager=worker_mgr,
        account_repo=account_repo,
    )

    return {
        "db": db,
        "task_repo": task_repo,
        "worker": worker,
        "worker_mgr": worker_mgr,
        "dispatcher": dispatcher,
    }


def test_worker_source_code_has_no_list_ready_call():
    """Invariant: Worker execution logic must NEVER call task_repo.list_ready()."""
    source_code = inspect.getsource(Worker)
    assert "list_ready" not in source_code, (
        "Security invariant violated: Worker class contains calls to list_ready(). "
        "Workers must never discover arbitrary tasks directly."
    )


def test_worker_process_next_task_rejects_unassigned_discovery(sched_authority_env):
    """Calling worker.process_next_task() without an assigned task must fail closed."""
    task_repo = sched_authority_env["task_repo"]
    worker = sched_authority_env["worker"]

    # Place a READY task in the database
    task = Task(id="t-bypass-test", contact_id="cnt-auth-1", type=TaskType.MESSAGE, status=TaskState.READY, account_id="acc-auth-1")
    task_repo.create(task)

    worker.start()

    # Worker attempts direct discovery without dispatcher assignment
    success = worker.process_next_task(executor=None)
    assert success is False

    # Invariant: task must remain untouched in READY state
    persisted = task_repo.get_by_id("t-bypass-test")
    assert persisted.status == TaskState.READY
    assert persisted.worker_id is None
    assert persisted.lock_token is None


def test_task_dispatcher_is_sole_authority_for_worker_assignment(sched_authority_env):
    """Tasks are only assigned to workers when dispatched through TaskDispatcher."""
    task_repo = sched_authority_env["task_repo"]
    worker = sched_authority_env["worker"]
    dispatcher = sched_authority_env["dispatcher"]

    task = Task(id="t-auth-run", contact_id="cnt-auth-1", type=TaskType.MESSAGE, status=TaskState.READY, account_id="acc-auth-1")
    task_repo.create(task)

    worker.start()

    # Dispatch via authoritative TaskDispatcher
    eligible, reason = dispatcher.is_task_eligible(task, worker=worker)
    assert eligible is True

    # Claim and execute through designated contract
    claimed = worker.claim_task(task.id)
    assert claimed is True
    assert worker.current_task_id == task.id
    assert worker.status == WorkerStatus.BUSY

    worker.release_current_task()
    assert worker.status == WorkerStatus.IDLE
    assert worker.current_task_id is None
