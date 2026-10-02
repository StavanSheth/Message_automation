"""Phase 5 Integration Tests: Crash Recovery, Self-Healing, and Reconciliation Safety."""

import pytest
from unittest.mock import MagicMock
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.task_repo import TaskRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.system_control_repo import SystemControlRepository
from backend.repositories.reconciliation_repo import ReconciliationRepository
from backend.repositories.manual_review_repo import ManualReviewRepository
from backend.repositories.execution_identity_repo import ExecutionIdentityRepository
from backend.reconciliation.service import ReconciliationService
from backend.application.lifecycle import ApplicationLifecycleManager
from backend.application.control_service import ApplicationControlService
from backend.workers.default_manager import DefaultWorkerManager
from backend.browser.manager import BrowserManager
from backend.domain.models import Task, Contact, ExecutionIdentity
from backend.domain.enums import SystemState, TaskState, TaskType, WorkerMode, WorkerStatus
from tests.fixtures.mock_browser import create_mock_session


@pytest.fixture
def recovery_env(tmp_path):
    db_file = str(tmp_path / "recovery_test.db")
    db = DatabaseManager(db_file)
    MigrationRunner(db).apply_pending()

    task_repo = TaskRepository(db)
    msg_repo = MessageRepository(db)
    contact_repo = ContactRepository(db)
    event_repo = EventRepository(db)
    control_repo = SystemControlRepository(db)
    rec_repo = ReconciliationRepository(db)
    mr_repo = ManualReviewRepository(db)
    exec_id_repo = ExecutionIdentityRepository(db)

    contact_repo.create(Contact(id="c-rec-1", name="Rec User", instagram_url="https://instagram.com/rec1"))

    rec_service = ReconciliationService(
        reconciliation_repo=rec_repo,
        task_repo=task_repo,
        message_repo=msg_repo,
        manual_review_repo=mr_repo,
        event_repo=event_repo,
    )

    lifecycle = ApplicationLifecycleManager(
        db=db,
        task_repo=task_repo,
        event_repo=event_repo,
        manual_review_repo=mr_repo,
        reconciliation_service=rec_service,
    )

    return {
        "db": db,
        "db_file": db_file,
        "task_repo": task_repo,
        "msg_repo": msg_repo,
        "contact_repo": contact_repo,
        "event_repo": event_repo,
        "control_repo": control_repo,
        "rec_repo": rec_repo,
        "rec_service": rec_service,
        "lifecycle": lifecycle,
        "exec_id_repo": exec_id_repo,
    }


def test_crash_with_persisted_running_state_recovers_to_degraded(recovery_env):
    """
    Section 6 Mandate:
    RUNNING on DB + no valid live runtime = startup recovery required.
    Do not blindly resume execution.
    """
    control_repo = recovery_env["control_repo"]
    lifecycle = recovery_env["lifecycle"]

    # Simulate crashed process leaving RUNNING in DB
    control_repo.set_state(SystemState.RUNNING)
    assert control_repo.get_state() == SystemState.RUNNING

    # New process starts up with no live workers
    ctrl = ApplicationControlService(lifecycle_manager=lifecycle, system_control_repo=control_repo)

    # Must NOT blindly remain RUNNING; must be DEGRADED pending recovery
    assert ctrl.state == SystemState.DEGRADED

    # Running startup recovery clears degraded state and resumes cleanly
    res = ctrl.start()
    assert res["status"] == "ok"
    assert ctrl.state == SystemState.RUNNING


def test_sending_and_verifying_tasks_route_to_reconciliation_on_restart(recovery_env):
    """
    Section 6 & 8 Mandate:
    Tasks in SENDING or VERIFYING when crash/restart occurs must route to RECONCILIATION,
    never automatically retry.
    """
    task_repo = recovery_env["task_repo"]
    rec_repo = recovery_env["rec_repo"]
    lifecycle = recovery_env["lifecycle"]

    # Seed task left in SENDING with expired lease
    t_send = task_repo.create(Task(id="task-crashed-send", contact_id="c-rec-1", type=TaskType.MESSAGE, sequence=0, status=TaskState.SENDING))
    t_verif = task_repo.create(Task(id="task-crashed-verif", contact_id="c-rec-1", type=TaskType.MESSAGE, sequence=1, status=TaskState.VERIFYING))

    # Run startup recovery
    summary = lifecycle.startup_recovery()

    t_send_after = task_repo.get_by_id("task-crashed-send")
    t_verif_after = task_repo.get_by_id("task-crashed-verif")

    # Tasks must be in RECONCILING, never READY
    assert t_send_after.status == TaskState.RECONCILING
    assert t_verif_after.status == TaskState.RECONCILING

    # Reconciliation records must exist
    rec_send = rec_repo.get_by_task_id("task-crashed-send")
    rec_verif = rec_repo.get_by_task_id("task-crashed-verif")
    assert rec_send is not None
    assert rec_send.state == "PENDING"
    assert rec_verif is not None
    assert rec_verif.state == "PENDING"


def test_worker_restart_during_sending_routes_to_reconciliation(recovery_env):
    """
    Section 11 Mandate:
    Worker restart is allowed only when no uncertain send.
    If current task is SENDING or VERIFYING, restart must first reconcile.
    """
    task_repo = recovery_env["task_repo"]
    event_repo = recovery_env["event_repo"]
    rec_service = recovery_env["rec_service"]
    rec_repo = recovery_env["rec_repo"]

    t = task_repo.create(Task(id="task-restart-send", contact_id="c-rec-1", type=TaskType.MESSAGE, status=TaskState.SENDING))

    worker_mgr = DefaultWorkerManager(
        task_repo=task_repo,
        event_repo=event_repo,
    )
    worker_mgr.reconciliation_service = rec_service

    # Start a worker
    worker_rec = worker_mgr.start_worker(mode=WorkerMode.SINGLE_BROWSER, worker_id="WKR-RESTART-1")
    worker = worker_mgr._workers["WKR-RESTART-1"]
    worker.current_task_id = "task-restart-send"

    # Request worker restart while task is in SENDING
    worker_mgr.restart_worker("WKR-RESTART-1")

    # Task must have entered reconciliation
    rec = rec_repo.get_by_task_id("task-restart-send")
    assert rec is not None
    assert rec.state == "PENDING"
    assert "worker_restart_during_send" in rec.reason


def test_browser_manager_recover_session(recovery_env):
    """
    Section 10 Mandate:
    Browser self-healing: detect, stop broken session, create replacement session.
    """
    event_repo = recovery_env["event_repo"]
    browser_mgr = BrowserManager(event_repo=event_repo)

    # Initial session
    s1 = browser_mgr.create_session(worker_id="WKR-SELF-HEAL")
    s1.start()
    assert s1.is_alive() is True
    s1_id = s1.session_id

    # Recover broken session
    s2 = browser_mgr.recover_session(worker_id="WKR-SELF-HEAL")
    assert s2.is_alive() is True
    assert s2.session_id != s1_id

    browser_mgr.shutdown()
