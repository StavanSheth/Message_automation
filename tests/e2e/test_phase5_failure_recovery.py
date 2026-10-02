"""Phase 5 E2E Tests: Ambiguous Send, Browser Crash, and Failure Recovery."""

import pytest
from unittest.mock import MagicMock
from backend.bootstrap import build_production_app
from backend.domain.models import Contact, Task, Message, Account
from backend.domain.enums import TaskType, TaskState, MessageState, SystemState, WorkerStatus
from tests.fixtures.realistic_browser_harness import create_realistic_session
from backend.browser.exceptions import BrowserCrashError


def test_ambiguous_send_routes_to_reconciliation(tmp_path):
    """
    Mandatory Safety Invariant:
    Ambiguous send result:
    SEND -> UNKNOWN RESULT -> RECONCILIATION -> MANUAL REVIEW if unresolved
    Never: UNKNOWN RESULT -> automatic retry -> second send
    """
    from tests.fixtures.realistic_browser_harness import RealisticBrowserDriverHarness
    db_file = str(tmp_path / "ambig_test.db")
    app = build_production_app(db_path=db_file, driver_factory=lambda: RealisticBrowserDriverHarness())

    app.contact_repo.create(Contact(id="c-amb-1", name="Amb User", instagram_url="https://instagram.com/amb1"))
    app.account_repo.create(Account(id="acc-amb-1", username="amb_user", status="ACTIVE"))

    app.task_repo.create(
        Task(
            id="task-amb-1",
            contact_id="c-amb-1",
            type=TaskType.MESSAGE,
            priority=10,
            status=TaskState.READY,
            account_id="acc-amb-1",
        )
    )
    app.message_repo.create(
        Message(
            id="msg-amb-1",
            contact_id="c-amb-1",
            task_id="task-amb-1",
            body="Ambiguous message",
            status=MessageState.PENDING,
        )
    )

    session = create_realistic_session("SESS-AMB-1")
    session.account_id = "acc-amb-1"
    session.start()

    worker_rec = app.worker_manager.start_worker(worker_id="WKR-AMB-1", account_id="acc-amb-1")
    worker = app.worker_manager._workers["WKR-AMB-1"]
    worker.session = session

    # Simulate ambiguous verification by having send_verifier return unknown
    app.automation_service.send_verifier.verify_sent_message = MagicMock(
        return_value={"confirmed": False, "reason": "Ambiguous DOM outcome"}
    )

    app.start()
    app.scheduler.tick()

    # Verify task entered RECONCILING, NOT COMPLETED and NOT direct retried
    t = app.task_repo.get_by_id("task-amb-1")
    assert t.status == TaskState.RECONCILING

    # Reconciliation record exists
    rec = app.reconciliation_repo.get_by_task_id("task-amb-1")
    assert rec is not None
    assert rec.state in ("PENDING", "IN_PROGRESS")

    app.stop()


def test_browser_crash_during_send_routes_to_reconciliation(tmp_path):
    """
    Mandatory Safety Invariant:
    Browser crash during SENDING / VERIFYING must route to RECONCILIATION,
    never blind automatic retry.
    """
    from tests.fixtures.realistic_browser_harness import RealisticBrowserDriverHarness
    db_file = str(tmp_path / "crash_send_test.db")
    app = build_production_app(db_path=db_file, driver_factory=lambda: RealisticBrowserDriverHarness())

    app.contact_repo.create(Contact(id="c-crsh-1", name="Crsh User", instagram_url="https://instagram.com/crsh1"))
    app.account_repo.create(Account(id="acc-crsh-1", username="crsh_user", status="ACTIVE"))

    app.task_repo.create(
        Task(
            id="task-crsh-1",
            contact_id="c-crsh-1",
            type=TaskType.MESSAGE,
            priority=10,
            status=TaskState.READY,
            account_id="acc-crsh-1",
        )
    )
    app.message_repo.create(
        Message(
            id="msg-crsh-1",
            contact_id="c-crsh-1",
            task_id="task-crsh-1",
            body="Crashing message",
            status=MessageState.PENDING,
        )
    )

    session = create_realistic_session("SESS-CRSH-1")
    session.account_id = "acc-crsh-1"
    session.start()

    worker_rec = app.worker_manager.start_worker(worker_id="WKR-CRSH-1", account_id="acc-crsh-1")
    worker = app.worker_manager._workers["WKR-CRSH-1"]
    worker.session = session

    # Simulate browser crash during execution
    def crash_during_send(*args, **kwargs):
        raise BrowserCrashError("Browser process disconnected unexpectedly during send")

    app.automation_service.execute_messaging_task = crash_during_send

    app.start()
    app.scheduler.tick()

    t = app.task_repo.get_by_id("task-crsh-1")
    assert t.status == TaskState.RECONCILING

    rec = app.reconciliation_repo.get_by_task_id("task-crsh-1")
    assert rec is not None
    assert rec.state in ("PENDING", "IN_PROGRESS")

    app.stop()
