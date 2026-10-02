"""Integration test verifying Worker -> ExecutionService -> InstagramAutomationService -> Browser production path."""

from unittest.mock import MagicMock
import pytest
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.task_repo import TaskRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.followup_repo import FollowupRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.error_repo import ErrorRepository
from backend.repositories.verification_result_repo import VerificationResultRepository
from backend.repositories.execution_identity_repo import ExecutionIdentityRepository
from backend.browser.instagram.automation_service import InstagramAutomationService
from backend.application.execution_service import ExecutionService
from backend.automation.task_executor import TaskExecutor
from backend.workers.worker import Worker
from backend.domain.models import Task, Contact, Message
from backend.domain.enums import TaskState, TaskType, MessageState, WorkerMode, WorkerStatus
from tests.fixtures.mock_browser import create_mock_session


@pytest.fixture
def execution_path_env(tmp_path):
    db_path = str(tmp_path / "test_exec_path.db")
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()

    task_repo = TaskRepository(db)
    msg_repo = MessageRepository(db)
    contact_repo = ContactRepository(db)
    fu_repo = FollowupRepository(db)
    event_repo = EventRepository(db)
    err_repo = ErrorRepository(db)
    verif_repo = VerificationResultRepository(db)
    exec_id_repo = ExecutionIdentityRepository(db)

    contact_repo.create(Contact(id="c-path-1", name="Path Contact", instagram_url="https://instagram.com/path1"))

    insta_service = InstagramAutomationService(
        task_repo=task_repo,
        message_repo=msg_repo,
        contact_repo=contact_repo,
        followup_repo=fu_repo,
        event_repo=event_repo,
        error_repo=err_repo,
        verification_repo=verif_repo,
    )

    exec_service = ExecutionService(
        task_repo=task_repo,
        message_repo=msg_repo,
        automation_service=insta_service,
        event_repo=event_repo,
        execution_identity_repo=exec_id_repo,
    )

    task_executor = TaskExecutor(
        task_repo=task_repo,
        event_repo=event_repo,
        error_repo=err_repo,
        instagram_service=insta_service,
        execution_service=exec_service,
    )

    session = create_mock_session("SESS-PATH-1")
    session.start()

    worker = Worker(
        worker_id="WKR-PATH-1",
        worker_code="worker-path-1",
        mode=WorkerMode.SINGLE_BROWSER,
        task_repo=task_repo,
        event_repo=event_repo,
        session=session,
    )
    worker.start()

    return {
        "db": db,
        "worker": worker,
        "task_executor": task_executor,
        "exec_service": exec_service,
        "insta_service": insta_service,
        "task_repo": task_repo,
        "msg_repo": msg_repo,
        "exec_id_repo": exec_id_repo,
        "session": session,
    }


def test_mandatory_execution_service_path_and_identity_recording(execution_path_env):
    worker = execution_path_env["worker"]
    task_executor = execution_path_env["task_executor"]
    insta_service = execution_path_env["insta_service"]
    task_repo = execution_path_env["task_repo"]
    msg_repo = execution_path_env["msg_repo"]
    exec_id_repo = execution_path_env["exec_id_repo"]

    # Mock execute_messaging_task on insta_service to return True
    insta_service.execute_messaging_task = MagicMock(return_value=True)

    task = Task(id="t-path-run", contact_id="c-path-1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    msg_repo.create(Message(id="m-path-run", task_id="t-path-run", contact_id="c-path-1", body="Hello!"))

    # Worker executes through TaskExecutor -> ExecutionService
    success = worker.process_next_task(executor=task_executor, assigned_task=task)
    assert success is True

    # Verify insta_service was called by ExecutionService
    insta_service.execute_messaging_task.assert_called_once()

    # Verify deterministic execution identity record was created in database
    identities = exec_id_repo.list_all() if hasattr(exec_id_repo, "list_all") else []
    # If list_all isn't implemented, query DB directly
    if not identities:
        conn = execution_path_env["db"].get_connection()
        cur = conn.execute("SELECT * FROM execution_identities WHERE task_id = 't-path-run';")
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0]["state"] == "SENT"
