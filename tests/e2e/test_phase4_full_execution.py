"""End-to-end tests for Phase 4 operational controls and complete execution chain."""

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
from backend.repositories.account_repo import AccountRepository
from backend.repositories.cooldown_repo import CooldownRepository
from backend.repositories.reconciliation_repo import ReconciliationRepository
from backend.repositories.manual_review_repo import ManualReviewRepository
from backend.browser.instagram.automation_service import InstagramAutomationService
from backend.application.execution_service import ExecutionService
from backend.application.throttling_service import ThrottlingService
from backend.application.control_service import ApplicationControlService
from backend.application.lifecycle import ApplicationLifecycleManager
from backend.automation.task_executor import TaskExecutor
from backend.workers.default_manager import DefaultWorkerManager
from backend.scheduler.scheduler import Scheduler
from backend.domain.models import Task, Contact, Message, Account
from backend.domain.enums import TaskState, TaskType, MessageState, WorkerMode, SystemState, AccountStatus, WorkerStatus
from tests.fixtures.realistic_browser_harness import create_realistic_session, RealisticBrowserDriverHarness


@pytest.fixture
def full_e2e_env(tmp_path):
    db_path = str(tmp_path / "test_full_e2e.db")
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
    account_repo = AccountRepository(db)
    cooldown_repo = CooldownRepository(db)
    rec_repo = ReconciliationRepository(db)
    review_repo = ManualReviewRepository(db)

    contact_repo.create(Contact(id="c-e2e-p4", name="E2E Contact", instagram_url="https://instagram.com/e2e_p4"))

    account_repo.create(
        Account(
            id="acc-e2e",
            username="bot_user",
            status=AccountStatus.ACTIVE.value,
            daily_send_limit=10,
            daily_sends_count=0,
        )
    )

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
        account_repo=account_repo,
    )

    task_executor = TaskExecutor(
        task_repo=task_repo,
        event_repo=event_repo,
        error_repo=err_repo,
        instagram_service=insta_service,
        execution_service=exec_service,
    )

    worker_mgr = DefaultWorkerManager(
        task_repo=task_repo,
        event_repo=event_repo,
        task_executor=task_executor,
    )

    throttling_svc = ThrottlingService(
        cooldown_repo=cooldown_repo,
        account_repo=account_repo,
        event_repo=event_repo,
    )

    lifecycle = ApplicationLifecycleManager(
        db=db,
        task_repo=task_repo,
        event_repo=event_repo,
        worker_manager=worker_mgr,
        manual_review_repo=review_repo,
    )

    control = ApplicationControlService(
        lifecycle_manager=lifecycle,
        worker_manager=worker_mgr,
        event_repo=event_repo,
    )

    scheduler = Scheduler(
        task_repo=task_repo,
        followup_repo=fu_repo,
        contact_repo=contact_repo,
        worker_manager=worker_mgr,
        control_service=control,
        throttling_service=throttling_svc,
    )

    session = create_realistic_session("SESS-E2E-P4")
    session.start()

    return {
        "db": db,
        "control": control,
        "scheduler": scheduler,
        "worker_mgr": worker_mgr,
        "task_repo": task_repo,
        "msg_repo": msg_repo,
        "account_repo": account_repo,
        "insta_service": insta_service,
        "session": session,
    }


def test_full_production_execution_chain(full_e2e_env):
    control = full_e2e_env["control"]
    scheduler = full_e2e_env["scheduler"]
    worker_mgr = full_e2e_env["worker_mgr"]
    task_repo = full_e2e_env["task_repo"]
    msg_repo = full_e2e_env["msg_repo"]
    account_repo = full_e2e_env["account_repo"]
    session = full_e2e_env["session"]

    # Start system via control service
    res = control.start()
    assert res["status"] == "ok"
    assert control.state == SystemState.RUNNING

    # Get worker started by control service and attach realistic session with strict ownership
    workers = worker_mgr.list_workers()
    worker = worker_mgr.get_worker(workers[0].id)
    worker.session = session
    worker.account_id = "acc-e2e"
    session.account_id = "acc-e2e"
    session.worker_id = worker.worker_id
    session.auth_status = "AUTHENTICATED"

    # Bind worker and session to account in account repository
    account_repo.assign_worker("acc-e2e", worker.worker_id, session.session_id)

    # Create task and message
    task = Task(id="t-p4-e2e", contact_id="c-e2e-p4", type=TaskType.MESSAGE, status=TaskState.READY, account_id="acc-e2e")
    task_repo.create(task)
    msg_repo.create(Message(id="m-p4-e2e", task_id="t-p4-e2e", contact_id="c-e2e-p4", body="Hello E2E!"))

    # Scheduler tick -> dispatches task to worker -> executes via ExecutionService -> InstagramAutomationService -> completes
    # Real DOM evaluation occurs through RealisticBrowserDriverHarness (NO MagicMock on evaluate)
    ready_tasks = scheduler.tick()
    assert len(ready_tasks) == 1

    # Verify task successfully completed
    t = task_repo.get_by_id("t-p4-e2e")
    assert t.status == TaskState.COMPLETED

    # Verify message sent and recorded in driver's thread
    m = msg_repo.get_by_id("m-p4-e2e")
    assert m.status == MessageState.SENT
    assert "Hello E2E!" in session.driver._sent_messages


def test_full_production_execution_challenge_detection(full_e2e_env):
    """Real E2E test verifying challenge page detection immediately routes to MANUAL_REVIEW and quarantines worker."""
    control = full_e2e_env["control"]
    control.start()

    scheduler = full_e2e_env["scheduler"]
    worker_mgr = full_e2e_env["worker_mgr"]
    task_repo = full_e2e_env["task_repo"]
    msg_repo = full_e2e_env["msg_repo"]
    account_repo = full_e2e_env["account_repo"]

    # Create worker with challenge session
    challenge_session = create_realistic_session("SESS-E2E-CHALLENGE")
    challenge_session.driver._page_type = "CHALLENGE"

    workers = worker_mgr.list_workers()
    worker = worker_mgr.get_worker(workers[0].id)
    worker.session = challenge_session
    worker.account_id = "acc-e2e"
    challenge_session.account_id = "acc-e2e"
    challenge_session.worker_id = worker.worker_id

    account_repo.assign_worker("acc-e2e", worker.worker_id, challenge_session.session_id)

    task = Task(id="t-p4-chall", contact_id="c-e2e-p4", type=TaskType.MESSAGE, status=TaskState.READY, account_id="acc-e2e")
    task_repo.create(task)
    msg_repo.create(Message(id="m-p4-chall", task_id="t-p4-chall", contact_id="c-e2e-p4", body="Challenge check"))

    # Scheduler tick -> dispatches task -> ExecutionService catches challenge -> quarantines worker & routes to MANUAL_REVIEW
    scheduler.tick()

    t = task_repo.get_by_id("t-p4-chall")
    assert t.status == TaskState.MANUAL_REVIEW
    assert worker.status == WorkerStatus.QUARANTINED
    assert "challenge" in str(worker.quarantine_reason).lower()

