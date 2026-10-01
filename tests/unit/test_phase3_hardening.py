"""Phase 3 Hardening unit tests covering:
- Execution identity deduplication in ExecutionService
- Pre-execution authentication validation in InstagramAutomationService
- Rate-limit cooldown tracking and task deferral in InstagramAutomationService
- Non-retryable CHALLENGE_REQUIRED error code in RetryScheduler / RetryPolicyEngine
- Graceful shutdown task lease release in ApplicationLifecycleManager
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from backend.domain.models import Task, Contact, Message, utc_now_iso
from backend.domain.enums import (
    TaskState,
    TaskType,
    MessageState,
    ErrorCode,
    EventCode,
    EventLevel,
    SessionAuthState,
    VerificationDecision,
    RepliedStatus,
)
from backend.browser.instagram.navigator import InstagramPageStatus
from backend.application.execution_service import ExecutionService
from backend.browser.instagram.automation_service import InstagramAutomationService
from backend.scheduler.retry_scheduler import RetryScheduler, RetryPolicyEngine, NON_RETRYABLE_CODES
from backend.application.lifecycle import ApplicationLifecycleManager
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.task_repo import TaskRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.followup_repo import FollowupRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.error_repo import ErrorRepository


@pytest.fixture
def db_repos(tmp_path):
    db = DatabaseManager(str(tmp_path / "test_hardening.db"))
    MigrationRunner(db).apply_pending()
    contact_repo = ContactRepository(db)
    contact_repo.create(
        Contact(
            id="C-HARD-1",
            name="Test User",
            instagram_url="https://instagram.com/test_user",
            replied_status=RepliedStatus.UNKNOWN,
        )
    )
    task_repo = TaskRepository(db)
    msg_repo = MessageRepository(db)
    followup_repo = FollowupRepository(db)
    event_repo = EventRepository(db)
    error_repo = ErrorRepository(db)
    return db, contact_repo, task_repo, msg_repo, followup_repo, event_repo, error_repo


# ── 1. Execution Identity Deduplication Tests ─────────────────────────────────

def test_execution_service_deduplicates_concurrent_identical_task():
    task_repo = MagicMock()
    message_repo = MagicMock()
    automation_service = MagicMock()

    service = ExecutionService(
        task_repo=task_repo,
        message_repo=message_repo,
        automation_service=automation_service,
    )

    task = Task(id="T-1", contact_id="C-1", type=TaskType.MESSAGE, status=TaskState.READY, sequence=0)
    task_repo.get_by_id.return_value = task
    task_repo.is_lease_valid.return_value = True

    # Compute key
    exec_key = service._compute_execution_key(task)
    assert len(exec_key) == 16

    # Simulate in-flight execution by manually adding to active set
    service._active_execution_keys.add(exec_key)

    mock_session = MagicMock()
    # Calling execute_task should immediately reject and return False
    result = service.execute_task(task_id="T-1", lease_id="L-1", worker_id="W-1", session=mock_session)
    assert result is False

    # After releasing, it should proceed past the deduplication check
    service._active_execution_keys.discard(exec_key)


def test_execution_service_cleans_up_key_after_execution():
    task_repo = MagicMock()
    message_repo = MagicMock()
    automation_service = MagicMock()

    service = ExecutionService(
        task_repo=task_repo,
        message_repo=message_repo,
        automation_service=automation_service,
    )

    task = Task(id="T-2", contact_id="C-1", type=TaskType.MESSAGE, status=TaskState.READY, sequence=0)
    task_repo.get_by_id.return_value = task
    # Fail lease to exit early inside try block
    task_repo.is_lease_valid.return_value = False

    mock_session = MagicMock()
    result = service.execute_task(task_id="T-2", lease_id="L-INVALID", worker_id="W-1", session=mock_session)
    assert result is False
    # Key must be cleared in finally block
    assert len(service._active_execution_keys) == 0


# ── 2. Pre-Execution Auth Validation Tests ────────────────────────────────────

def test_instagram_automation_pre_execution_auth_login_required(db_repos):
    db, contact_repo, task_repo, msg_repo, followup_repo, event_repo, error_repo = db_repos

    task = Task(id="T-AUTH-1", contact_id="C-HARD-1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    msg = Message(id="M-AUTH-1", contact_id="C-HARD-1", task_id="T-AUTH-1", sequence=0, body="Hi", status=MessageState.PENDING)
    msg_repo.create(msg)

    auth_validator = MagicMock()
    auth_validator.check_auth_state.return_value = (SessionAuthState.LOGIN_REQUIRED, "login_form_present")

    service = InstagramAutomationService(
        task_repo=task_repo,
        contact_repo=contact_repo,
        message_repo=msg_repo,
        followup_repo=followup_repo,
        error_repo=error_repo,
        event_repo=event_repo,
        auth_validator=auth_validator,
    )

    mock_session = MagicMock()
    mock_session.is_alive.return_value = True

    result = service.execute_messaging_task(task, mock_session, worker_id="W-1")
    assert result is False

    updated_task = task_repo.get_by_id("T-AUTH-1")
    assert updated_task.status == TaskState.MANUAL_REVIEW

    # Error and event recorded
    errors = error_repo.list_by_task("T-AUTH-1")
    assert any(e.code == ErrorCode.SESSION_EXPIRED for e in errors)

    events = event_repo.list_events(entity_id="T-AUTH-1")
    assert any(e.event_code == EventCode.LOGIN_REQUIRED for e in events)


def test_instagram_automation_pre_execution_auth_challenge(db_repos):
    db, contact_repo, task_repo, msg_repo, followup_repo, event_repo, error_repo = db_repos

    task = Task(id="T-AUTH-2", contact_id="C-HARD-1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    msg = Message(id="M-AUTH-2", contact_id="C-HARD-1", task_id="T-AUTH-2", sequence=0, body="Hi", status=MessageState.PENDING)
    msg_repo.create(msg)

    auth_validator = MagicMock()
    auth_validator.check_auth_state.return_value = (SessionAuthState.CHALLENGE, "checkpoint_screen")

    service = InstagramAutomationService(
        task_repo=task_repo,
        contact_repo=contact_repo,
        message_repo=msg_repo,
        followup_repo=followup_repo,
        error_repo=error_repo,
        event_repo=event_repo,
        auth_validator=auth_validator,
    )

    mock_session = MagicMock()
    mock_session.is_alive.return_value = True

    result = service.execute_messaging_task(task, mock_session, worker_id="W-1")
    assert result is False

    updated_task = task_repo.get_by_id("T-AUTH-2")
    assert updated_task.status == TaskState.MANUAL_REVIEW

    errors = error_repo.list_by_task("T-AUTH-2")
    assert any(e.code == ErrorCode.CHALLENGE_REQUIRED for e in errors)


# ── 3. Rate-Limit Cooldown Tracking Tests ─────────────────────────────────────

def test_rate_limit_cooldown_defers_task(db_repos):
    db, contact_repo, task_repo, msg_repo, followup_repo, event_repo, error_repo = db_repos

    task = Task(id="T-COOL-1", contact_id="C-HARD-1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    msg = Message(id="M-COOL-1", contact_id="C-HARD-1", task_id="T-COOL-1", sequence=0, body="Hi", status=MessageState.PENDING)
    msg_repo.create(msg)

    service = InstagramAutomationService(
        task_repo=task_repo,
        contact_repo=contact_repo,
        message_repo=msg_repo,
        followup_repo=followup_repo,
        error_repo=error_repo,
        event_repo=event_repo,
    )

    # Set active cooldown
    service._rate_limit_cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=120)

    mock_session = MagicMock()
    mock_session.is_alive.return_value = True

    result = service.execute_messaging_task(task, mock_session, worker_id="W-1")
    assert result is False

    updated_task = task_repo.get_by_id("T-COOL-1")
    assert updated_task.status == TaskState.RETRY_WAIT

    errors = error_repo.list_by_task("T-COOL-1")
    assert any(e.code == ErrorCode.RATE_LIMITED for e in errors)


def test_rate_limit_cooldown_activated_on_access_blocked(db_repos):
    db, contact_repo, task_repo, msg_repo, followup_repo, event_repo, error_repo = db_repos

    task = Task(id="T-COOL-2", contact_id="C-HARD-1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    msg = Message(id="M-COOL-2", contact_id="C-HARD-1", task_id="T-COOL-2", sequence=0, body="Hi", status=MessageState.PENDING)
    msg_repo.create(msg)

    auth_validator = MagicMock()
    auth_validator.check_auth_state.return_value = (SessionAuthState.AUTHENTICATED, "ok")

    navigator = MagicMock()
    navigator.navigate_to_profile.return_value = {"status": InstagramPageStatus.ACCESS_BLOCKED}

    service = InstagramAutomationService(
        task_repo=task_repo,
        contact_repo=contact_repo,
        message_repo=msg_repo,
        followup_repo=followup_repo,
        error_repo=error_repo,
        event_repo=event_repo,
        auth_validator=auth_validator,
        navigator=navigator,
    )

    mock_session = MagicMock()
    mock_session.is_alive.return_value = True

    assert service._rate_limit_cooldown_until is None

    result = service.execute_messaging_task(task, mock_session, worker_id="W-1")
    assert result is False
    assert service._rate_limit_cooldown_until is not None
    assert service._rate_limit_cooldown_until > datetime.now(timezone.utc)


def test_rate_limit_cooldown_activated_on_action_blocked_send(db_repos):
    db, contact_repo, task_repo, msg_repo, followup_repo, event_repo, error_repo = db_repos

    task = Task(id="T-COOL-3", contact_id="C-HARD-1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    msg = Message(id="M-COOL-3", contact_id="C-HARD-1", task_id="T-COOL-3", sequence=0, body="Hi", status=MessageState.PENDING)
    msg_repo.create(msg)

    auth_validator = MagicMock()
    auth_validator.check_auth_state.return_value = (SessionAuthState.AUTHENTICATED, "ok")

    navigator = MagicMock()
    navigator.navigate_to_profile.return_value = {"status": InstagramPageStatus.AVAILABLE}

    reader = MagicMock()
    reader.extract_profile.return_value = {"username": "test_user", "can_message": True}

    verifier = MagicMock()
    verifier.verify_profile.return_value = (VerificationDecision.HIGH_CONFIDENCE, 1.0, {}, None)
    verifier.is_send_allowed.return_value = True

    composer = MagicMock()
    composer.open_message_dialog.return_value = {"success": True}
    composer.compose_message.return_value = {"success": True}

    sender = MagicMock()
    sender.submit_send.return_value = {
        "submitted": False,
        "error_code": ErrorCode.ACTION_BLOCKED,
        "reason": "Action blocked by Instagram",
    }

    service = InstagramAutomationService(
        task_repo=task_repo,
        contact_repo=contact_repo,
        message_repo=msg_repo,
        followup_repo=followup_repo,
        error_repo=error_repo,
        event_repo=event_repo,
        auth_validator=auth_validator,
        navigator=navigator,
        reader=reader,
        verifier=verifier,
        composer=composer,
        sender=sender,
    )

    mock_session = MagicMock()
    mock_session.is_alive.return_value = True

    assert service._rate_limit_cooldown_until is None

    result = service.execute_messaging_task(task, mock_session, worker_id="W-1")
    assert result is False
    assert service._rate_limit_cooldown_until is not None
    assert service._rate_limit_cooldown_until > datetime.now(timezone.utc)


# ── 4. Non-retryable CHALLENGE_REQUIRED in RetryScheduler ─────────────────────

def test_challenge_required_is_non_retryable():
    assert ErrorCode.CHALLENGE_REQUIRED in NON_RETRYABLE_CODES

    retry_class, retryable, max_attempts, mult = RetryPolicyEngine.classify_error(ErrorCode.CHALLENGE_REQUIRED)
    assert retryable is False
    assert max_attempts == 0


# ── 5. Graceful Shutdown Lease Release Tests ──────────────────────────────────

def test_graceful_shutdown_releases_active_task_leases():
    db = MagicMock()
    worker_manager = MagicMock()
    task_repo = MagicMock()
    browser_manager = MagicMock()

    w1 = MagicMock()
    w1.current_task_id = "TASK-RUNNING-1"
    w2 = MagicMock()
    w2.current_task_id = None

    worker_manager.list_workers.return_value = [w1, w2]

    lifecycle = ApplicationLifecycleManager(
        db=db,
        task_repo=task_repo,
        worker_manager=worker_manager,
        browser_manager=browser_manager,
    )

    lifecycle.graceful_shutdown()

    # Must have unlocked the running worker's task
    task_repo.unlock_task.assert_called_once_with("TASK-RUNNING-1")
    worker_manager.stop_all.assert_called_once()
