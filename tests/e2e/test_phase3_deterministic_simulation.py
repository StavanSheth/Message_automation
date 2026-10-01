"""Phase 3 end-to-end deterministic simulation of the complete automation pipeline (Section 33)."""

import pytest
from unittest.mock import MagicMock
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.task_repo import TaskRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.followup_repo import FollowupRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.error_repo import ErrorRepository
from backend.repositories.verification_result_repo import VerificationResultRepository
from backend.browser.session import BrowserSessionInstance
from backend.browser.instagram.automation_service import InstagramAutomationService
from backend.application.followup_service import FollowupService
from backend.application.execution_service import ExecutionService
from backend.domain.models import Contact, Task, Message, Followup
from backend.domain.enums import TaskState, TaskType, MessageState, FollowupStatus, RepliedStatus
from backend.config.settings import AppSettings


@pytest.fixture
def e2e_system(tmp_path):
    db = DatabaseManager(str(tmp_path / "e2e_sim.db"))
    MigrationRunner(db).apply_pending()
    contact_repo = ContactRepository(db)
    task_repo = TaskRepository(db)
    msg_repo = MessageRepository(db)
    fu_repo = FollowupRepository(db)
    evt_repo = EventRepository(db)
    err_repo = ErrorRepository(db)
    verif_repo = VerificationResultRepository(db)

    followup_service = FollowupService(
        followup_repo=fu_repo,
        task_repo=task_repo,
        contact_repo=contact_repo,
        message_repo=msg_repo,
        event_repo=evt_repo,
    )

    automation_service = InstagramAutomationService(
        task_repo=task_repo,
        contact_repo=contact_repo,
        message_repo=msg_repo,
        followup_repo=fu_repo,
        event_repo=evt_repo,
        error_repo=err_repo,
        verification_repo=verif_repo,
        followup_service=followup_service,
        settings=AppSettings(execution_mode="AUTOMATIC", verification_threshold=0.8),
    )

    execution_service = ExecutionService(
        task_repo=task_repo,
        message_repo=msg_repo,
        automation_service=automation_service,
        event_repo=evt_repo,
    )

    return {
        "db": db,
        "contact_repo": contact_repo,
        "task_repo": task_repo,
        "msg_repo": msg_repo,
        "fu_repo": fu_repo,
        "evt_repo": evt_repo,
        "followup_service": followup_service,
        "execution_service": execution_service,
    }


def test_full_pipeline_simulation_send_to_reply_cancellation(e2e_system):
    contact_repo = e2e_system["contact_repo"]
    task_repo = e2e_system["task_repo"]
    msg_repo = e2e_system["msg_repo"]
    fu_repo = e2e_system["fu_repo"]
    fu_service = e2e_system["followup_service"]
    exec_service = e2e_system["execution_service"]

    # 1. Contact, Task, Message, Followups created from source sync
    contact = Contact(
        id="C-SIM-1",
        name="Jordan Lee",
        instagram_url="https://www.instagram.com/jordanlee/",
        username="jordanlee",
        expected_followers=5000,
        replied_status=RepliedStatus.UNKNOWN,
    )
    contact_repo.create(contact)

    task = Task(id="T-SIM-1", contact_id=contact.id, type=TaskType.MESSAGE, sequence=0, status=TaskState.READY)
    task_repo.create(task)

    msg = Message(
        id="M-SIM-1",
        contact_id=contact.id,
        task_id=task.id,
        sequence=0,
        body="Hi Jordan, let's collaborate!",
        status=MessageState.PENDING,
    )
    msg_repo.create(msg)

    fu1 = Followup(
        id="FU-SIM-1",
        contact_id=contact.id,
        sequence=1,
        message="Quick check-in!",
        delay_seconds=3600,
        scheduled_at="2026-01-01T00:00:00Z",
        status=FollowupStatus.PENDING,
    )
    fu_repo.create(fu1)

    # 2. Worker acquires lease
    worker_id = "WORKER-SIM-1"
    lease_id = task_repo.acquire_lease(task.id, worker_id, lease_duration_seconds=120)
    assert lease_id is not None

    # 3. Mock Browser Session
    mock_session = MagicMock(spec=BrowserSessionInstance)
    mock_session.is_alive.return_value = True
    mock_session.navigate.return_value = "https://www.instagram.com/jordanlee/"
    mock_session.evaluate.side_effect = [
        {"status": "AVAILABLE"},  # Navigator
        {                         # Profile Reader
            "url": "https://www.instagram.com/jordanlee/",
            "username": "jordanlee",
            "display_name": "Jordan Lee",
            "follower_count_text": "5,000",
            "can_message": True,
        },
        {"success": True},        # Open DM dialog
        True,                     # Composer ready check
        {"success": True},        # Composer typing
        {"submitted": True},      # Submit send
        {"found": True, "snippet": "Hi Jordan, let's collaborate!"}, # Send verifier
    ]

    # 4. Authoritative execution
    success = exec_service.execute_task(
        task_id=task.id,
        session=mock_session,
        worker_id=worker_id,
        lease_id=lease_id,
        correlation_id="CORR-SIM-001",
    )
    assert success is True

    # 5. Verify task completed and message marked SENT
    updated_task = task_repo.get_by_id(task.id)
    assert updated_task.status == TaskState.COMPLETED

    updated_msg = msg_repo.get_by_id(msg.id)
    assert updated_msg.status == MessageState.SENT
    assert updated_msg.confirmed_at is not None

    # 6. Verify Followup 1 transitioned from PENDING to SCHEDULED
    updated_fu = fu_repo.get_by_id(fu1.id)
    assert updated_fu.status == FollowupStatus.SCHEDULED

    # 7. Contact replies YES -> cancel followups
    contact_repo.update_replied_status(contact.id, RepliedStatus.YES)
    fu_service.cancel_pending_followups(contact.id, reason="REPLIED")

    cancelled_fu = fu_repo.get_by_id(fu1.id)
    assert cancelled_fu.status == FollowupStatus.CANCELLED
    assert cancelled_fu.cancel_reason == "REPLIED"
