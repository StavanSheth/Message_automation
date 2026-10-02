"""Phase 5 E2E Tests: Comprehensive Duplicate Send and Double Execution Prevention."""

import sqlite3
import pytest
from backend.bootstrap import build_production_app
from backend.domain.models import Contact, Task, Message, Account, ExecutionIdentity
from backend.domain.enums import TaskType, TaskState, MessageState, SystemState, WorkerStatus
from tests.fixtures.realistic_browser_harness import create_realistic_session


def test_completed_task_triggers_no_second_send(tmp_path):
    """
    Mandatory Safety Invariant:
    A task that has already reached COMPLETED or SENT must NEVER send a second message.
    """
    db_file = str(tmp_path / "dup_test.db")
    app = build_production_app(db_path=db_file)

    app.contact_repo.create(Contact(id="c-dup-1", name="Dup User", instagram_url="https://instagram.com/dup1"))
    app.account_repo.create(Account(id="acc-dup-1", username="dup_user", status="ACTIVE"))

    app.task_repo.create(
        Task(
            id="task-dup-1",
            contact_id="c-dup-1",
            type=TaskType.MESSAGE,
            priority=10,
            status=TaskState.READY,
            account_id="acc-dup-1",
        )
    )
    app.message_repo.create(
        Message(
            id="msg-dup-1",
            contact_id="c-dup-1",
            task_id="task-dup-1",
            body="First send body",
            status=MessageState.PENDING,
        )
    )

    session = create_realistic_session("SESS-DUP-1")
    session.account_id = "acc-dup-1"
    session.start()

    worker_rec = app.worker_manager.start_worker(worker_id="WKR-DUP-1", account_id="acc-dup-1")
    worker = app.worker_manager._workers["WKR-DUP-1"]
    worker.session = session

    app.start()
    app.scheduler.tick()

    # Verify first execution sent
    t = app.task_repo.get_by_id("task-dup-1")
    assert t.status == TaskState.COMPLETED
    m = app.message_repo.get_by_id("msg-dup-1")
    assert m.status == MessageState.SENT

    driver = session.driver
    assert len(driver._sent_messages) == 1

    # Second execution attempt via execution service directly
    res = app.execution_service.execute_task(
        task_id="task-dup-1",
        session=session,
        worker_id="WKR-DUP-1",
        lease_id="LEASE-FAKED",
    )
    # Must NOT send again
    assert len(driver._sent_messages) == 1

    app.stop()


def test_database_triggers_prevent_sent_reversals(tmp_path):
    """
    Database Invariant:
    Verify DB triggers trg_tasks_prevent_sent_reversal and trg_execution_identities_prevent_sent_reversal
    prevent state tampering at the storage engine level.
    """
    db_file = str(tmp_path / "trigger_test.db")
    app = build_production_app(db_path=db_file)

    app.contact_repo.create(Contact(id="c-trg-1", name="Trg User", instagram_url="https://instagram.com/trg1"))
    app.task_repo.create(Task(id="task-trg-1", contact_id="c-trg-1", type=TaskType.MESSAGE, status=TaskState.COMPLETED))

    # 1. Attempt to illegally revert COMPLETED task to READY in database
    conn = app.db.get_connection()
    with pytest.raises(sqlite3.IntegrityError) as exc_info:
        with app.db.transaction() as tx:
            tx.execute("UPDATE tasks SET status = 'READY' WHERE id = 'task-trg-1';")
    assert "Illegal task state transition" in str(exc_info.value)

    # 2. Seed SENT execution identity
    app.execution_identity_repo.create(
        ExecutionIdentity(
            execution_key="EXEC-KEY-SENT",
            task_id="task-trg-1",
            contact_id="c-trg-1",
            message_hash="hash123",
            attempt=1,
            state="SENT",
            outcome="CONFIRMED_SENT",
        )
    )

    # Attempt to reopen SENT execution identity to RUNNING
    with pytest.raises(sqlite3.IntegrityError) as exc_info2:
        with app.db.transaction() as tx:
            tx.execute("UPDATE execution_identities SET state = 'RUNNING' WHERE execution_key = 'EXEC-KEY-SENT';")
    assert "Illegal execution identity transition" in str(exc_info2.value)
