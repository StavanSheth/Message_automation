"""
Deterministic End-to-End Messaging Pipeline and Regression Tests for Phase 2.

Covers:
1. Complete End-to-End messaging pipeline:
   Spreadsheet -> Contact -> Task -> Scheduler -> Worker -> Instagram Navigation ->
   Profile Verification -> Message Composition -> Message Send -> Send Verification ->
   COMPLETED -> Follow-up 1 Scheduling.
2. Retry handling:
   Deterministic distinction between retryable (timeout, crash) and non-retryable
   (mismatch, not found) errors.
3. Regression test for duplicate task bug in source_service.py.
4. Follow-up cancellation on contact reply.
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
import pytest

from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.task_repo import TaskRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.error_repo import ErrorRepository
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.source_record_repo import SourceRecordRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.followup_repo import FollowupRepository
from backend.repositories.sync_run_repo import SyncRunRepository
from backend.repositories.worker_repo import WorkerRepository
from backend.repositories.verification_result_repo import VerificationResultRepository
from backend.application.source_service import SourceService
from backend.automation.task_executor import TaskExecutor
from backend.automation.execution_context import ExecutionContext
from backend.scheduler.service import SchedulerFoundationService
from backend.workers.default_manager import DefaultWorkerManager
from backend.browser.instagram.automation_service import InstagramAutomationService
from backend.domain.models import Task, Contact, Message, Followup
from backend.domain.enums import (
    TaskState,
    TaskType,
    MessageState,
    FollowupStatus,
    RepliedStatus,
    WorkerStatus,
    WorkerMode,
    VerificationDecision,
    ErrorCode,
    EventCode,
)
from backend.config.settings import reset_settings, get_settings


@pytest.fixture(autouse=True)
def clean_env():
    reset_settings()
    yield
    reset_settings()


@pytest.fixture
def messaging_env(tmp_path):
    db_path = str(tmp_path / "e2e_messaging.db")
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()

    contact_repo = ContactRepository(db)
    srec_repo = SourceRecordRepository(db)
    task_repo = TaskRepository(db)
    msg_repo = MessageRepository(db)
    fu_repo = FollowupRepository(db)
    evt_repo = EventRepository(db)
    sync_repo = SyncRunRepository(db)
    err_repo = ErrorRepository(db)
    worker_repo = WorkerRepository(db)
    verif_repo = VerificationResultRepository(db)

    source_service = SourceService(
        contact_repo=contact_repo,
        source_record_repo=srec_repo,
        task_repo=task_repo,
        message_repo=msg_repo,
        followup_repo=fu_repo,
        event_repo=evt_repo,
        sync_run_repo=sync_repo,
    )

    insta_service = InstagramAutomationService(
        task_repo=task_repo,
        message_repo=msg_repo,
        contact_repo=contact_repo,
        followup_repo=fu_repo,
        event_repo=evt_repo,
        error_repo=err_repo,
        verification_repo=verif_repo,
    )

    task_executor = TaskExecutor(
        task_repo=task_repo,
        event_repo=evt_repo,
        error_repo=err_repo,
        source_service=source_service,
        instagram_service=insta_service,
    )

    worker_manager = DefaultWorkerManager(
        task_repo=task_repo,
        event_repo=evt_repo,
        task_executor=task_executor,
        worker_repo=worker_repo,
    )

    scheduler = SchedulerFoundationService(
        task_repo=task_repo,
        followup_repo=fu_repo,
        contact_repo=contact_repo,
        worker_manager=worker_manager,
    )

    return {
        "db": db,
        "contact_repo": contact_repo,
        "srec_repo": srec_repo,
        "task_repo": task_repo,
        "msg_repo": msg_repo,
        "fu_repo": fu_repo,
        "evt_repo": evt_repo,
        "err_repo": err_repo,
        "worker_repo": worker_repo,
        "verif_repo": verif_repo,
        "source_service": source_service,
        "insta_service": insta_service,
        "task_executor": task_executor,
        "worker_manager": worker_manager,
        "scheduler": scheduler,
    }


def test_full_e2e_pipeline_message_to_followup_scheduling(messaging_env):
    """
    Test deterministic mocked-browser E2E pipeline:
    Contact -> Task (READY) -> Scheduler tick -> Worker claim ->
    Instagram mock execution -> Send verification -> COMPLETED -> Follow-up 1 scheduled.
    """
    env = messaging_env
    contact_repo = env["contact_repo"]
    task_repo = env["task_repo"]
    msg_repo = env["msg_repo"]
    fu_repo = env["fu_repo"]
    worker_manager = env["worker_manager"]
    scheduler = env["scheduler"]
    insta_service = env["insta_service"]

    # 1. Setup contact, task, message, and followup 1
    contact = Contact(
        id="c-e2e-1",
        name="Alice Wonderland",
        instagram_url="https://www.instagram.com/alice_wonder",
        replied_status=RepliedStatus.NO,
    )
    contact_repo.create(contact)

    task = Task(
        id="t-e2e-msg-1",
        contact_id="c-e2e-1",
        type=TaskType.MESSAGE,
        sequence=0,
        status=TaskState.READY,
        scheduled_at=datetime.now(timezone.utc).isoformat(),
    )
    task_repo.create(task)

    message = Message(
        id="m-e2e-msg-1",
        contact_id="c-e2e-1",
        task_id="t-e2e-msg-1",
        body="Hello Alice, this is a verified automation message!",
        status=MessageState.PENDING,
    )
    msg_repo.create(message)

    fu1 = Followup(
        id="fu-e2e-1",
        contact_id="c-e2e-1",
        sequence=1,
        message="Follow up message",
        delay_seconds=86400,
        scheduled_at=datetime.now(timezone.utc).isoformat(),
        status=FollowupStatus.PENDING,
    )
    fu_repo.create(fu1)

    # 2. Register an idle worker
    w_rec = worker_manager.start_worker(mode=WorkerMode.SINGLE_BROWSER)
    worker = worker_manager.get_worker(w_rec.id)
    from tests.fixtures.mock_browser import create_mock_session
    worker.session = create_mock_session(session_id="sess-e2e-1", worker_id=worker.worker_id)
    assert worker.status == WorkerStatus.IDLE

    # Mock the internal browser operations of InstagramAutomationService
    with patch.object(insta_service.navigator, "navigate_to_profile", return_value={"status": "AVAILABLE", "url": "https://www.instagram.com/alice_wonder/"}), \
         patch.object(insta_service.reader, "extract_profile", return_value={
             "username": "alice_wonder",
             "display_name": "Alice Wonderland",
             "follower_count": 1200,
             "exists": True,
             "can_message": True,
         }), \
         patch.object(insta_service.composer, "open_message_dialog", return_value={"success": True}), \
         patch.object(insta_service.composer, "compose_message", return_value={"success": True}), \
         patch.object(insta_service.sender, "submit_send", return_value={"submitted": True}), \
         patch.object(insta_service.send_verifier, "verify_sent_message", return_value={"confirmed": True}):

        # 3. Scheduler tick claims and dispatches to worker
        dispatched = scheduler.tick()
        assert len(dispatched) >= 1

        # 4. Verify message state is SENT
        updated_msg = msg_repo.get_by_id("m-e2e-msg-1")
        assert updated_msg is not None
        assert updated_msg.status == MessageState.SENT

        # 5. Verify task state is COMPLETED
        updated_task = task_repo.get_by_id("t-e2e-msg-1")
        assert updated_task is not None
        assert updated_task.status == TaskState.COMPLETED
        assert updated_task.lock_token is None

        # 6. Verify Followup 1 status progressed to SCHEDULED
        updated_fu1 = fu_repo.get_by_id("fu-e2e-1")
        assert updated_fu1 is not None
        assert updated_fu1.status == FollowupStatus.SCHEDULED
        assert updated_fu1.scheduled_at is not None

        # Worker returned to IDLE
        assert worker.status == WorkerStatus.IDLE


def test_retry_policy_retryable_vs_non_retryable(messaging_env):
    """
    Test deterministic retry behavior:
    1. Retryable failure (browser timeout/network failure) -> Task state transitions to RETRY_WAIT,
       attempt_count incremented, error recorded.
    2. Non-retryable failure (profile mismatch) -> Task state transitions to MANUAL_REVIEW,
       NEVER retried automatically.
    """
    env = messaging_env
    contact_repo = env["contact_repo"]
    task_repo = env["task_repo"]
    msg_repo = env["msg_repo"]
    fu_repo = env["fu_repo"]
    worker_manager = env["worker_manager"]
    scheduler = env["scheduler"]
    insta_service = env["insta_service"]

    # Contact 1: Will experience a retryable error (timeout/network)
    contact1 = Contact(id="c-retry-1", name="Bob", instagram_url="https://instagram.com/bob_retry")
    contact_repo.create(contact1)
    task1 = Task(id="t-retry-1", contact_id="c-retry-1", type=TaskType.MESSAGE, status=TaskState.READY, scheduled_at=datetime.now(timezone.utc).isoformat())
    task_repo.create(task1)
    msg1 = Message(id="m-retry-1", contact_id="c-retry-1", task_id="t-retry-1", body="Hi Bob")
    msg_repo.create(msg1)

    # Contact 2: Will experience a non-retryable error (identity mismatch)
    contact2 = Contact(id="c-mismatch-2", name="Charlie Brown", instagram_url="https://instagram.com/charlie_brown")
    contact_repo.create(contact2)
    task2 = Task(id="t-mismatch-2", contact_id="c-mismatch-2", type=TaskType.MESSAGE, status=TaskState.READY, scheduled_at=datetime.now(timezone.utc).isoformat())
    task_repo.create(task2)
    msg2 = Message(id="m-mismatch-2", contact_id="c-mismatch-2", task_id="t-mismatch-2", body="Hi Charlie")
    msg_repo.create(msg2)

    w_rec = worker_manager.start_worker(mode=WorkerMode.SINGLE_BROWSER)
    worker = worker_manager.get_worker(w_rec.id)
    from tests.fixtures.mock_browser import create_mock_session
    worker.session = create_mock_session(session_id="sess-retry-1", worker_id=worker.worker_id)

    # 1. Execute task 1 with retryable timeout
    with patch.object(insta_service.navigator, "navigate_to_profile", return_value={"status": "UNAVAILABLE", "error": "Page load timeout"}):

        worker.claim_task("t-retry-1")
        ctx = ExecutionContext(task_id="t-retry-1", worker_id=worker.worker_id)
        success = env["task_executor"].execute_task(task=task1, context=ctx, session=worker.session)
        worker.release_current_task()

        assert success is False
        t1_updated = task_repo.get_by_id("t-retry-1")
        assert t1_updated.status == TaskState.RETRY_WAIT
        assert t1_updated.attempt_count >= 1

    # 2. Execute task 2 with verification MISMATCH
    with patch.object(insta_service.navigator, "navigate_to_profile", return_value={"status": "AVAILABLE", "url": "https://www.instagram.com/charlie_brown/"}), \
         patch.object(insta_service.reader, "extract_profile", return_value={
             "username": "completely_different_user",
             "display_name": "Totally Unrelated",
             "follower_count": 0,
             "exists": True,
             "can_message": True,
         }):

        worker.claim_task("t-mismatch-2")
        ctx = ExecutionContext(task_id="t-mismatch-2", worker_id=worker.worker_id)
        success = env["task_executor"].execute_task(task=task2, context=ctx, session=worker.session)
        worker.release_current_task()

        assert success is False
        t2_updated = task_repo.get_by_id("t-mismatch-2")
        # Identity mismatch must escalate to MANUAL_REVIEW, NEVER retried
        assert t2_updated.status == TaskState.MANUAL_REVIEW


def test_duplicate_task_bug_regression_in_source_service(messaging_env):
    """
    Regression test for Section 18:
    When a duplicate task is detected during source import/sync:
    - Retrieve existing task
    - Use existing task.id
    - Do NOT crash with DuplicateTaskError
    - Do NOT create an orphan message
    """
    env = messaging_env
    source_service = env["source_service"]
    contact_repo = env["contact_repo"]
    task_repo = env["task_repo"]
    msg_repo = env["msg_repo"]

    # Pre-create contact and existing task
    contact = Contact(id="c-dup-1", name="Duplicate Test Contact", instagram_url="https://instagram.com/dup_user")
    contact_repo.create(contact)

    existing_task = Task(
        id="t-existing-1",
        contact_id="c-dup-1",
        type=TaskType.MESSAGE,
        sequence=0,
        status=TaskState.READY,
        scheduled_at=datetime.now(timezone.utc).isoformat(),
    )
    task_repo.create(existing_task)

    existing_msg = Message(
        id="m-existing-1",
        contact_id="c-dup-1",
        task_id="t-existing-1",
        body="Original message body",
        status=MessageState.PENDING,
    )
    msg_repo.create(existing_msg)

    # Now simulate a source sync that attempts to import this same contact and message
    mock_source = MagicMock()
    mock_source.source_type = "MOCK"
    mock_source.source_identifier = "mock_source_1"
    mock_source.fetch_records.return_value = [
        {
            "row_index": 2,
            "name": "Duplicate Test Contact",
            "instagram_url": "https://instagram.com/dup_user",
            "message": "Original message body",
            "followup_1": "FU1 text",
            "followup_2": "FU2 text",
        }
    ]

    sync_run = source_service.sync_source(mock_source)

    # Verify no crash, sync succeeded
    assert sync_run.status.value in ("SUCCESS", "RUNNING")

    # Verify no duplicate task was created
    contact_tasks = task_repo.get_by_contact_id("c-dup-1")
    message_tasks = [t for t in contact_tasks if t.type == TaskType.MESSAGE]
    assert len(message_tasks) == 1
    assert message_tasks[0].id == "t-existing-1"

    # Verify no orphan message was created
    messages = msg_repo.list_by_contact("c-dup-1")
    assert len(messages) == 1
    assert messages[0].task_id == "t-existing-1"


def test_contact_reply_cancels_pending_and_scheduled_followups(messaging_env):
    """
    Test follow-up cancellation:
    When a contact is marked REPLIED = YES:
    - All pending/scheduled follow-ups must be cancelled.
    - Cancelled follow-up must never execute or be scheduled by the scheduler.
    """
    env = messaging_env
    contact_repo = env["contact_repo"]
    fu_repo = env["fu_repo"]
    task_repo = env["task_repo"]
    scheduler = env["scheduler"]

    contact = Contact(id="c-reply-cancel", name="Replied Contact", instagram_url="https://instagram.com/replied_c")
    contact_repo.create(contact)

    # Create scheduled followup due right now
    fu = Followup(
        id="fu-due-1",
        contact_id="c-reply-cancel",
        sequence=1,
        message="Follow up message",
        delay_seconds=86400,
        status=FollowupStatus.SCHEDULED,
        scheduled_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    )
    fu_repo.create(fu)

    # Contact replies!
    contact.replied_status = RepliedStatus.YES
    contact_repo.update(contact)

    # Run scheduler tick
    scheduler.tick()

    # The follow-up must be CANCELLED and never dispatched
    updated_fu = fu_repo.get_by_id("fu-due-1")
    assert updated_fu.status == FollowupStatus.CANCELLED

    # Verify no follow-up task was created
    contact_tasks = task_repo.get_by_contact_id("c-reply-cancel")
    fu_tasks = [t for t in contact_tasks if t.type == TaskType.FOLLOW_UP_1]
    assert len(fu_tasks) == 0
