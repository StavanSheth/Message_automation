"""Unit tests for Instagram browser automation components:
- InstagramNavigator
- InstagramProfileReader
- InstagramProfileVerifier
- InstagramMessageComposer
- InstagramMessageSender
- InstagramSendVerifier
- InstagramAutomationService
"""

import json
from unittest.mock import MagicMock
import pytest

from backend.browser.session import BrowserSessionInstance
from backend.browser.instagram.navigator import InstagramNavigator, InstagramPageStatus
from backend.browser.instagram.profile_reader import InstagramProfileReader, parse_metric_string
from backend.browser.instagram.profile_verifier import InstagramProfileVerifier
from backend.browser.instagram.message_composer import InstagramMessageComposer
from backend.browser.instagram.message_sender import InstagramMessageSender
from backend.browser.instagram.send_verifier import InstagramSendVerifier
from backend.browser.instagram.automation_service import InstagramAutomationService

from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.task_repo import TaskRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.followup_repo import FollowupRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.error_repo import ErrorRepository
from backend.repositories.verification_result_repo import VerificationResultRepository

from backend.domain.models import Contact, Task, Message, Followup
from backend.domain.enums import (
    TaskType,
    TaskState,
    MessageState,
    FollowupStatus,
    VerificationDecision,
    RepliedStatus,
    ErrorCode,
)
from backend.config.settings import AppSettings


@pytest.fixture
def db_repos(tmp_path):
    db = DatabaseManager(str(tmp_path / "insta_test.db"))
    MigrationRunner(db).apply_pending()
    return {
        "db": db,
        "contact_repo": ContactRepository(db),
        "task_repo": TaskRepository(db),
        "message_repo": MessageRepository(db),
        "followup_repo": FollowupRepository(db),
        "event_repo": EventRepository(db),
        "error_repo": ErrorRepository(db),
        "verification_repo": VerificationResultRepository(db),
    }


def test_navigator_url_normalization():
    nav = InstagramNavigator()
    # Handle @username
    assert nav.normalize_url("@alice") == "https://www.instagram.com/alice/"
    # Handle bare username
    assert nav.normalize_url("bob") == "https://www.instagram.com/bob/"
    # Handle domain without protocol
    assert nav.normalize_url("instagram.com/charlie") == "https://www.instagram.com/charlie/"
    # Handle http and trailing query params
    assert nav.normalize_url("http://instagram.com/diana?hl=en#section") == "https://www.instagram.com/diana/"
    # Extract username
    assert nav.extract_username_from_url("https://www.instagram.com/edward/") == "edward"


def test_navigator_page_state_detection():
    nav = InstagramNavigator()
    mock_session = MagicMock(spec=BrowserSessionInstance)
    mock_session.is_alive.return_value = True
    mock_session.navigate.return_value = "https://www.instagram.com/alice/"

    # Case 1: Available profile
    mock_session.evaluate.return_value = {"status": "AVAILABLE"}
    res = nav.navigate_to_profile(mock_session, "alice")
    assert res["status"] == InstagramPageStatus.AVAILABLE
    assert res["username"] == "alice"

    # Case 2: Login required
    mock_session.evaluate.return_value = {"status": "LOGIN_REQUIRED", "reason": "login_form"}
    res = nav.navigate_to_profile(mock_session, "alice")
    assert res["status"] == InstagramPageStatus.LOGIN_REQUIRED

    # Case 3: Page not found
    mock_session.evaluate.return_value = {"status": "NOT_FOUND", "reason": "page_not_found"}
    res = nav.navigate_to_profile(mock_session, "alice")
    assert res["status"] == InstagramPageStatus.NOT_FOUND

    # Case 4: Restricted
    mock_session.evaluate.return_value = {"status": "RESTRICTED", "reason": "profile_restricted"}
    res = nav.navigate_to_profile(mock_session, "alice")
    assert res["status"] == InstagramPageStatus.RESTRICTED


def test_profile_reader_metric_parsing():
    assert parse_metric_string("1,234") == 1234
    assert parse_metric_string("10.5K") == 10500
    assert parse_metric_string("1.2M") == 1200000
    assert parse_metric_string("invalid") is None
    assert parse_metric_string("") is None


def test_profile_reader_extraction():
    reader = InstagramProfileReader()
    mock_session = MagicMock(spec=BrowserSessionInstance)
    mock_session.is_alive.return_value = True
    mock_session.evaluate.return_value = {
        "url": "https://www.instagram.com/sarah/",
        "username": "sarah",
        "display_name": "Sarah Connor",
        "follower_count_text": "5,400",
        "following_count_text": "350",
        "post_count_text": "85",
        "bio": "Bio content",
        "is_verified": True,
        "is_private": False,
        "can_message": True,
        "page_missing": False,
    }

    profile = reader.extract_profile(mock_session)
    assert profile["username"] == "sarah"
    assert profile["display_name"] == "Sarah Connor"
    assert profile["follower_count"] == 5400
    assert profile["is_verified"] is True
    assert profile["can_message"] is True


def test_profile_verifier_and_audit_persistence(db_repos):
    contact_repo = db_repos["contact_repo"]
    verification_repo = db_repos["verification_repo"]

    contact = Contact(
        id="C-VER-1",
        name="Sarah Connor",
        instagram_url="https://www.instagram.com/sarah/",
        username="sarah",
        expected_followers=5000,
    )
    contact_repo.create(contact)

    verifier = InstagramProfileVerifier(verification_repo=verification_repo, threshold=0.85)

    # 1. Matching observed data -> HIGH_CONFIDENCE
    observed_match = {
        "url": "https://www.instagram.com/sarah",
        "username": "sarah",
        "display_name": "Sarah Connor",
        "follower_count": 5100,
        "can_message": True,
    }
    decision, conf, signals, rec = verifier.verify_profile(contact, observed_match, task_id=None)
    assert decision == VerificationDecision.HIGH_CONFIDENCE
    assert conf >= 0.85
    assert verifier.is_send_allowed(decision, execution_mode="AUTOMATIC") is True

    # Audit record persisted
    saved = verification_repo.get_by_id(rec.id)
    assert saved is not None
    assert saved.decision == VerificationDecision.HIGH_CONFIDENCE

    # 2. Total mismatch -> MISMATCH, send NEVER allowed
    observed_mismatch = {
        "url": "https://www.instagram.com/stranger",
        "username": "stranger",
        "display_name": "Different Person",
        "follower_count": 50,
        "can_message": True,
    }
    decision_m, conf_m, _, _ = verifier.verify_profile(contact, observed_mismatch)
    assert decision_m == VerificationDecision.MISMATCH
    assert verifier.is_send_allowed(decision_m, execution_mode="AUTOMATIC") is False
    assert verifier.is_send_allowed(decision_m, execution_mode="MANUAL") is False


def test_message_composer_and_sender():
    mock_session = MagicMock(spec=BrowserSessionInstance)
    mock_session.is_alive.return_value = True

    # Test Composer open message dialog
    composer = InstagramMessageComposer()
    mock_session.evaluate.side_effect = [
        {"success": True},  # open_message_dialog button click
        True,               # ready check
        {"success": True, "text_entered": True},  # compose_message
    ]
    res_dialog = composer.open_message_dialog(mock_session)
    assert res_dialog["success"] is True

    res_compose = composer.compose_message(mock_session, "Hello Sarah!")
    assert res_compose["success"] is True

    # Test Sender
    sender = InstagramMessageSender()
    mock_session.evaluate.side_effect = None
    mock_session.evaluate.return_value = {"submitted": True}
    res_send = sender.submit_send(mock_session)
    assert res_send["submitted"] is True
    assert res_send["error_code"] is None


def test_send_verifier_positive_and_negative():
    verifier = InstagramSendVerifier()
    mock_session = MagicMock(spec=BrowserSessionInstance)
    mock_session.is_alive.return_value = True

    # Case 1: Confirmed delivery
    mock_session.evaluate.return_value = {"found": True, "snippet": "Hello Sarah!", "failure_indicator": False}
    res = verifier.verify_sent_message(mock_session, "Hello Sarah!")
    assert res["confirmed"] is True
    assert res["message_state"] == MessageState.SENT
    assert res["task_state"] == TaskState.COMPLETED

    # Case 2: Failure indicator present
    mock_session.evaluate.return_value = {"found": False, "failure_indicator": True}
    res_fail = verifier.verify_sent_message(mock_session, "Hello Sarah!")
    assert res_fail["confirmed"] is False
    assert res_fail["message_state"] == MessageState.FAILED

    # Case 3: Ambiguous / not found in thread (fails closed)
    mock_session.evaluate.return_value = {"found": False, "failure_indicator": False}
    res_ambig = verifier.verify_sent_message(mock_session, "Hello Sarah!")
    assert res_ambig["confirmed"] is False
    assert res_ambig["message_state"] == MessageState.RECONCILIATION
    assert res_ambig["task_state"] == TaskState.MANUAL_REVIEW


def test_instagram_automation_service_full_flow(db_repos):
    """Verify complete end-to-end messaging pipeline through InstagramAutomationService."""
    contact_repo = db_repos["contact_repo"]
    task_repo = db_repos["task_repo"]
    message_repo = db_repos["message_repo"]
    followup_repo = db_repos["followup_repo"]
    event_repo = db_repos["event_repo"]
    error_repo = db_repos["error_repo"]
    verification_repo = db_repos["verification_repo"]

    contact = Contact(
        id="C-FLOW-1",
        name="Alex River",
        instagram_url="https://www.instagram.com/alexriver/",
        username="alexriver",
        expected_followers=1000,
        replied_status=RepliedStatus.UNKNOWN,
    )
    contact_repo.create(contact)

    task = Task(
        id="T-FLOW-1",
        contact_id=contact.id,
        type=TaskType.MESSAGE,
        sequence=0,
        status=TaskState.READY,
    )
    task_repo.create(task)

    msg = Message(
        id="M-FLOW-1",
        contact_id=contact.id,
        task_id=task.id,
        sequence=0,
        body="Hi Alex, reaching out regarding collaboration.",
        status=MessageState.PENDING,
    )
    message_repo.create(msg)

    fu1 = Followup(
        id="FU-FLOW-1",
        contact_id=contact.id,
        sequence=1,
        message="Checking back with you!",
        delay_seconds=3600,
        scheduled_at="2026-01-01T00:00:00Z",
        status=FollowupStatus.PENDING,
    )
    followup_repo.create(fu1)

    # Setup mocked browser session
    mock_session = MagicMock(spec=BrowserSessionInstance)
    mock_session.is_alive.return_value = True
    mock_session.navigate.return_value = "https://www.instagram.com/alexriver/"

    # Mock DOM evaluate calls for:
    # 1. Navigator check -> AVAILABLE
    # 2. Reader extract -> valid profile with can_message: True
    # 3. Composer open dialog -> success
    # 4. Composer ready check -> True
    # 5. Composer type -> success
    # 6. Sender submit -> success
    # 7. Send verifier -> found match
    mock_session.evaluate.side_effect = [
        {"state": "AUTHENTICATED", "reason": "navigation_elements_present"},  # Auth validator
        {"status": "AVAILABLE"},  # Navigator
        {                         # Reader
            "url": "https://www.instagram.com/alexriver/",
            "username": "alexriver",
            "display_name": "Alex River",
            "follower_count_text": "1,050",
            "can_message": True,
        },
        {"success": True},        # Composer open dialog
        True,                     # Composer ready check
        {"success": True},        # Composer type
        {"submitted": True},      # Sender submit
        {"found": True, "snippet": "Hi Alex, reaching out"},  # SendVerifier
    ]

    service = InstagramAutomationService(
        task_repo=task_repo,
        contact_repo=contact_repo,
        message_repo=message_repo,
        followup_repo=followup_repo,
        event_repo=event_repo,
        error_repo=error_repo,
        verification_repo=verification_repo,
        settings=AppSettings(execution_mode="AUTOMATIC", verification_threshold=0.85),
    )

    success = service.execute_messaging_task(task, mock_session, worker_id="W-1")
    assert success is True

    # Verify Task updated to COMPLETED
    updated_task = task_repo.get_by_id(task.id)
    assert updated_task.status == TaskState.COMPLETED
    assert updated_task.completed_at is not None

    # Verify Message updated to SENT
    updated_msg = message_repo.get_by_id(msg.id)
    assert updated_msg.status == MessageState.SENT
    assert updated_msg.confirmed_at is not None

    # Verify Follow-up 1 scheduled
    updated_fu = followup_repo.get_by_id(fu1.id)
    assert updated_fu.status == FollowupStatus.SCHEDULED
    assert updated_fu.scheduled_at > "2026-01-01"
