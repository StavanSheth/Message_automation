"""Phase 5 Integration Tests: Concurrency, Atomic Leases, and Duplicate Send Protection."""

import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock
import pytest

from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.task_repo import TaskRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.error_repo import ErrorRepository
from backend.repositories.verification_result_repo import VerificationResultRepository
from backend.repositories.execution_identity_repo import ExecutionIdentityRepository
from backend.browser.instagram.automation_service import InstagramAutomationService
from backend.application.execution_service import ExecutionService
from backend.domain.models import Task, Contact, Message
from backend.domain.enums import TaskState, TaskType, MessageState, WorkerMode
from backend.workers.worker import Worker
from tests.fixtures.mock_browser import create_mock_session


@pytest.fixture
def concurrency_env(tmp_path):
    db_file = str(tmp_path / "concurrency_test.db")
    db = DatabaseManager(db_file)
    MigrationRunner(db).apply_pending()

    task_repo = TaskRepository(db)
    msg_repo = MessageRepository(db)
    contact_repo = ContactRepository(db)
    event_repo = EventRepository(db)
    err_repo = ErrorRepository(db)
    verif_repo = VerificationResultRepository(db)
    exec_id_repo = ExecutionIdentityRepository(db)

    # Seed contact
    contact_repo.create(Contact(id="c-conc-1", name="Conc User", instagram_url="https://instagram.com/conc1"))

    # Automation service with spy on execute_messaging_task
    insta_svc = InstagramAutomationService(
        task_repo=task_repo,
        contact_repo=contact_repo,
        message_repo=msg_repo,
        followup_repo=MagicMock(),
        event_repo=event_repo,
        error_repo=err_repo,
        verification_repo=verif_repo,
    )
    send_counter = {"count": 0}

    def fake_send(task, session, worker_id, correlation_id=None):
        send_counter["count"] += 1
        return True

    insta_svc.execute_messaging_task = fake_send

    exec_service = ExecutionService(
        task_repo=task_repo,
        message_repo=msg_repo,
        automation_service=insta_svc,
        event_repo=event_repo,
        execution_identity_repo=exec_id_repo,
    )

    return {
        "db": db,
        "task_repo": task_repo,
        "msg_repo": msg_repo,
        "contact_repo": contact_repo,
        "event_repo": event_repo,
        "exec_id_repo": exec_id_repo,
        "exec_service": exec_service,
        "insta_svc": insta_svc,
        "send_counter": send_counter,
    }


def test_two_workers_simultaneously_claim_same_task(concurrency_env):
    """
    Section 22 Mandate:
    Worker A and Worker B simultaneously attempt to claim the same task.
    Expected:
    Worker A -> execution owner
    Worker B -> rejected
    Exactly one worker acquires the task.
    """
    task_repo = concurrency_env["task_repo"]
    event_repo = concurrency_env["event_repo"]
    task_repo.create(Task(id="task-race-1", contact_id="c-conc-1", type=TaskType.MESSAGE, status=TaskState.READY))

    worker_a = Worker(
        worker_id="WKR-A",
        worker_code="worker-a",
        mode=WorkerMode.SINGLE_BROWSER,
        task_repo=task_repo,
        event_repo=event_repo,
    )
    worker_b = Worker(
        worker_id="WKR-B",
        worker_code="worker-b",
        mode=WorkerMode.SINGLE_BROWSER,
        task_repo=task_repo,
        event_repo=event_repo,
    )

    results = {}
    barrier = threading.Barrier(2)

    def attempt_claim(worker, name):
        barrier.wait()
        res = worker.claim_task("task-race-1")
        results[name] = res

    t1 = threading.Thread(target=attempt_claim, args=(worker_a, "A"))
    t2 = threading.Thread(target=attempt_claim, args=(worker_b, "B"))

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Exactly one worker must succeed, the other must be rejected
    assert (results["A"] and not results["B"]) or (results["B"] and not results["A"])

    winner_id = "WKR-A" if results["A"] else "WKR-B"
    task = task_repo.get_by_id("task-race-1")
    assert task.status == TaskState.RUNNING
    assert task.worker_id == winner_id
    assert task.lease_owner == winner_id


def test_two_simultaneous_execution_service_calls_deduplicate(concurrency_env):
    """
    Section 22 Mandate:
    Two simultaneous ExecutionService calls with the same task and same execution identity.
    Expected:
    one execution
    one send
    """
    task_repo = concurrency_env["task_repo"]
    msg_repo = concurrency_env["msg_repo"]
    exec_service = concurrency_env["exec_service"]
    send_counter = concurrency_env["send_counter"]

    task = task_repo.create(Task(id="task-double-1", contact_id="c-conc-1", type=TaskType.MESSAGE, status=TaskState.READY))
    msg_repo.create(Message(id="msg-double-1", contact_id="c-conc-1", task_id="task-double-1", body="Hello double send test", status=MessageState.PENDING))

    # Pre-acquire lease for worker 1
    lease_id = task_repo.acquire_lease("task-double-1", worker_id="WKR-EXEC-1")
    assert lease_id is not None

    session = create_mock_session("SESS-DEDUP")
    session.start()

    exec_results = []
    barrier = threading.Barrier(2)

    def execute_call():
        barrier.wait()
        res = exec_service.execute_task(
            task_id="task-double-1",
            session=session,
            worker_id="WKR-EXEC-1",
            lease_id=lease_id,
        )
        exec_results.append(res)

    t1 = threading.Thread(target=execute_call)
    t2 = threading.Thread(target=execute_call)

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Exactly one send attempt was made by the browser automation service
    assert send_counter["count"] == 1
    # At least one call returned True (or second was rejected as duplicate in-flight)
    assert any(exec_results)
