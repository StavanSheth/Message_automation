"""Phase 5 End-to-End Tests: Complete Production Path Execution."""

import pytest
from unittest.mock import MagicMock
from backend.bootstrap import build_production_app
from backend.domain.models import Contact, Task, Message, Account
from backend.domain.enums import TaskType, TaskState, MessageState, SystemState, WorkerStatus, EventCode
from tests.fixtures.realistic_browser_harness import create_realistic_session


def test_full_production_pipeline_execution(tmp_path):
    """
    End-to-End verification of the complete production messaging path:
    build_production_app -> start -> Scheduler -> TaskDispatcher -> Worker -> ExecutionService -> Browser -> Sent
    """
    db_file = str(tmp_path / "e2e_prod.db")
    app = build_production_app(db_path=db_file)

    # Validate graph first
    is_valid, errors = app.validate_dependency_graph()
    assert is_valid is True

    # Seed contact and account
    app.contact_repo.create(Contact(id="c-e2e-1", name="E2E User", instagram_url="https://instagram.com/e2e1"))
    app.account_repo.create(Account(id="acc-e2e-1", username="e2e_user", status="ACTIVE"))

    # Seed task and message
    app.task_repo.create(
        Task(
            id="task-e2e-1",
            contact_id="c-e2e-1",
            type=TaskType.MESSAGE,
            priority=10,
            status=TaskState.READY,
            account_id="acc-e2e-1",
        )
    )
    app.message_repo.create(
        Message(
            id="msg-e2e-1",
            contact_id="c-e2e-1",
            task_id="task-e2e-1",
            body="Hello E2E Phase 5 test",
            status=MessageState.PENDING,
        )
    )

    # Attach realistic mock browser session to worker
    session = create_realistic_session("SESS-E2E-P5")
    session.account_id = "acc-e2e-1"
    session.start()

    # Start worker through worker manager
    worker_rec = app.worker_manager.start_worker(worker_id="WKR-E2E-1", account_id="acc-e2e-1")
    worker = app.worker_manager._workers["WKR-E2E-1"]
    worker.session = session
    worker.status = WorkerStatus.IDLE

    # Start the production application
    res = app.start()
    assert res["status"] == "ok"
    assert app.control_service.state == SystemState.RUNNING

    # Tick scheduler loop once
    app.scheduler.tick()

    # Verify task completed
    t = app.task_repo.get_by_id("task-e2e-1")
    assert t.status == TaskState.COMPLETED

    # Verify message sent
    m = app.message_repo.get_by_id("msg-e2e-1")
    assert m.status == MessageState.SENT

    # Verify execution identity recorded in DB
    exec_id = app.execution_identity_repo.get_by_task_id("task-e2e-1")
    assert exec_id is not None
    assert exec_id.state == "SENT"
    assert exec_id.outcome == "CONFIRMED_SENT"

    # Stop app gracefully
    app.stop()
    assert app.control_service.state == SystemState.STOPPED
