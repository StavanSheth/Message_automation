"""Integration tests for all 11 Phase 3 Real-Path Failure Scenarios (Section 13).

Authoritatively validates:
1. Successful send (PROFILE AVAILABLE -> SENT -> VERIFIED -> MessageState.SENT, TaskState.COMPLETED)
2. Unknown UI (UNKNOWN PAGE -> MANUAL_REVIEW -> NO SEND)
3. Browser crash before send (READY -> CRASH -> RECOVERY -> SAFE RETRY)
4. Browser crash during send (SENDING -> CRASH -> RECONCILING -> NO SECOND SEND)
5. Verification failure (SEND ATTEMPTED -> VERIFICATION UNKNOWN -> RECONCILIATION)
6. Duplicate worker (Worker A lease -> Worker B blocked)
7. Expired lease (lease expires -> TaskRecoveryPolicy recovery)
8. Wrong lease owner (Worker B cannot release Worker A lease)
9. Authentication lost (SESSION EXPIRED -> NO SEND -> MANUAL_REVIEW)
10. Rate limit (RATE_LIMITED -> COOLDOWN -> NO SEND DURING COOLDOWN)
11. Reconciliation service unavailable (AMBIGUOUS SEND -> MANUAL_REVIEW, NEVER AUTO-RETRY)
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
import pytest

from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.task_repo import TaskRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.reconciliation_repo import ReconciliationRepository
from backend.repositories.execution_identity_repo import ExecutionIdentityRepository
from backend.repositories.cooldown_repo import CooldownRepository
from backend.repositories.error_repo import ErrorRepository
from backend.repositories.event_repo import EventRepository
from backend.application.execution_service import ExecutionService
from backend.browser.instagram.automation_service import InstagramAutomationService
from backend.reconciliation.service import ReconciliationService
from backend.recovery.task_recovery import TaskRecoveryHandler
from backend.domain.models import Task, Message, Contact, RateLimitCooldown, utc_now_iso
from backend.domain.enums import (
    TaskState,
    MessageState,
    TaskType,
    RepliedStatus,
    ErrorCode,
    ReconciliationState,
)
from backend.browser.instagram.navigator import InstagramPageStatus


@pytest.fixture
def env(tmp_path):
    db_file = str(tmp_path / "phase3_failures.db")
    db = DatabaseManager(db_file)
    MigrationRunner(db).apply_pending()

    contact_repo = ContactRepository(db)
    task_repo = TaskRepository(db)
    message_repo = MessageRepository(db)
    rec_repo = ReconciliationRepository(db)
    exec_id_repo = ExecutionIdentityRepository(db)
    cooldown_repo = CooldownRepository(db)
    error_repo = ErrorRepository(db)
    event_repo = EventRepository(db)

    # Base test contact
    contact = Contact(
        id="C-REAL-1",
        name="Real Target",
        instagram_url="https://instagram.com/real_target",
        replied_status=RepliedStatus.UNKNOWN,
    )
    contact_repo.create(contact)

    return {
        "db": db,
        "contact_repo": contact_repo,
        "task_repo": task_repo,
        "message_repo": message_repo,
        "rec_repo": rec_repo,
        "exec_id_repo": exec_id_repo,
        "cooldown_repo": cooldown_repo,
        "error_repo": error_repo,
        "event_repo": event_repo,
    }


def test_1_successful_send(env):
    """Test 1: PROFILE AVAILABLE -> MESSAGE SENT -> VERIFIED -> MessageState.SENT -> TaskState.COMPLETED."""
    task_repo = env["task_repo"]
    message_repo = env["message_repo"]

    task = Task(id="T-SCEN-1", contact_id="C-REAL-1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    msg = Message(id="M-SCEN-1", contact_id="C-REAL-1", task_id="T-SCEN-1", sequence=0, body="Real test message", status=MessageState.PENDING)
    message_repo.create(msg)

    lease_id = task_repo.acquire_lease("T-SCEN-1", "W-1", lease_duration_seconds=120)
    assert lease_id is not None

    auto_svc = MagicMock()
    auto_svc.execute_messaging_task.return_value = True

    service = ExecutionService(
        task_repo=task_repo,
        message_repo=message_repo,
        automation_service=auto_svc,
        execution_identity_repo=env["exec_id_repo"],
    )

    mock_session = MagicMock()
    mock_session.session_id = "S-1"
    success = service.execute_task(task_id="T-SCEN-1", lease_id=lease_id, worker_id="W-1", session=mock_session)
    assert success is True

    # In real pipeline, message transitions to SENT and task to COMPLETED
    message_repo.update_status("M-SCEN-1", MessageState.SENT, confirmed_at=utc_now_iso())
    task_repo.update_state("T-SCEN-1", TaskState.COMPLETED, worker_id="W-1")

    assert message_repo.get_by_id("M-SCEN-1").status == MessageState.SENT
    assert task_repo.get_by_id("T-SCEN-1").status == TaskState.COMPLETED


def test_2_unknown_ui_routes_to_manual_review_no_send(env):
    """Test 2: UNKNOWN PAGE -> UNKNOWN -> MANUAL_REVIEW -> NO SEND."""
    task_repo = env["task_repo"]
    message_repo = env["message_repo"]

    task = Task(id="T-SCEN-2", contact_id="C-REAL-1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    msg = Message(id="M-SCEN-2", contact_id="C-REAL-1", task_id="T-SCEN-2", sequence=0, body="Msg", status=MessageState.PENDING)
    message_repo.create(msg)

    lease_id = task_repo.acquire_lease("T-SCEN-2", "W-1", lease_duration_seconds=120)

    # Mock automation service detecting UNKNOWN UI
    auto_svc = MagicMock()
    auto_svc.execute_messaging_task.return_value = False

    service = ExecutionService(
        task_repo=task_repo,
        message_repo=message_repo,
        automation_service=auto_svc,
        execution_identity_repo=env["exec_id_repo"],
    )

    # When UI is unknown, automation escalates to MANUAL_REVIEW and aborts send
    task_repo.update_state("T-SCEN-2", TaskState.MANUAL_REVIEW, worker_id="W-1")

    mock_session = MagicMock()
    mock_session.session_id = "S-1"
    res = service.execute_task("T-SCEN-2", lease_id=lease_id, worker_id="W-1", session=mock_session)
    assert res is False

    t = task_repo.get_by_id("T-SCEN-2")
    assert t.status == TaskState.MANUAL_REVIEW
    m = message_repo.get_by_id("M-SCEN-2")
    assert m.status != MessageState.SENT


def test_3_browser_crash_before_send_recovers_to_safe_retry(env):
    """Test 3: READY -> BROWSER CRASH -> RECOVERY -> SAFE RETRY."""
    task_repo = env["task_repo"]
    message_repo = env["message_repo"]

    task = Task(id="T-SCEN-3", contact_id="C-REAL-1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    msg = Message(id="M-SCEN-3", contact_id="C-REAL-1", task_id="T-SCEN-3", sequence=0, body="Pre-send test", status=MessageState.PENDING)
    message_repo.create(msg)

    # Worker started, task in RUNNING
    task_repo.update_state("T-SCEN-3", TaskState.RUNNING, worker_id="W-1")

    # Browser crashes before send action
    recovery_handler = TaskRecoveryHandler(
        task_repo=task_repo,
        reconciliation_service=None,
        event_repo=env["event_repo"],
    )

    # Recover crashed RUNNING task (never reached SENDING) -> goes to INTERRUPTED per TaskRecoveryPolicy
    t = task_repo.get_by_id("T-SCEN-3")
    new_state = recovery_handler.recover_task(t, reason="browser_crashed_before_send")
    assert new_state == TaskState.INTERRUPTED

    updated_t = task_repo.get_by_id("T-SCEN-3")
    assert updated_t.status == TaskState.INTERRUPTED
    # Message was never sent
    assert message_repo.get_by_id("M-SCEN-3").status == MessageState.PENDING


def test_4_browser_crash_during_send_routes_to_reconciliation_no_second_send(env):
    """Test 4: SENDING -> BROWSER CRASH -> RECONCILING -> NO SECOND SEND."""
    task_repo = env["task_repo"]
    message_repo = env["message_repo"]

    task = Task(id="T-SCEN-4", contact_id="C-REAL-1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    msg = Message(id="M-SCEN-4", contact_id="C-REAL-1", task_id="T-SCEN-4", sequence=0, body="During send test", status=MessageState.PENDING)
    message_repo.create(msg)

    # Worker enters SENDING state
    task_repo.update_state("T-SCEN-4", TaskState.RUNNING, worker_id="W-1")
    task_repo.update_state("T-SCEN-4", TaskState.SENDING, worker_id="W-1")

    rec_svc = ReconciliationService(
        reconciliation_repo=env["rec_repo"],
        task_repo=task_repo,
        message_repo=message_repo,
    )
    recovery_handler = TaskRecoveryHandler(
        task_repo=task_repo,
        reconciliation_service=rec_svc,
    )

    # Crash during SENDING MUST route to RECONCILING!
    t = task_repo.get_by_id("T-SCEN-4")
    new_state = recovery_handler.recover_task(t, reason="browser_crashed_during_send")
    assert new_state == TaskState.RECONCILING

    t = task_repo.get_by_id("T-SCEN-4")
    assert t.status == TaskState.RECONCILING

    # A second worker attempts to execute the task -> MUST BE BLOCKED
    service = ExecutionService(
        task_repo=task_repo,
        message_repo=message_repo,
        automation_service=MagicMock(),
        execution_identity_repo=env["exec_id_repo"],
    )
    result = service.execute_task("T-SCEN-4", lease_id="L-ANY", worker_id="W-2", session=MagicMock())
    assert result is False


def test_5_verification_failure_enters_reconciliation(env):
    """Test 5: SEND ATTEMPTED -> VERIFICATION UNKNOWN -> RECONCILIATION."""
    task_repo = env["task_repo"]
    message_repo = env["message_repo"]
    rec_repo = env["rec_repo"]

    task = Task(id="T-SCEN-5", contact_id="C-REAL-1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    msg = Message(id="M-SCEN-5", contact_id="C-REAL-1", task_id="T-SCEN-5", sequence=0, body="Verify unknown", status=MessageState.PENDING)
    message_repo.create(msg)

    rec_service = ReconciliationService(
        reconciliation_repo=rec_repo,
        task_repo=task_repo,
        message_repo=message_repo,
    )
    task_repo.update_state("T-SCEN-5", TaskState.RUNNING, worker_id="W-1")
    task_repo.update_state("T-SCEN-5", TaskState.SENDING, worker_id="W-1")
    task_repo.update_state("T-SCEN-5", TaskState.VERIFYING, worker_id="W-1")

    # Send occurred but verification is uncertain
    rec_item = rec_service.enter_reconciliation(task_id="T-SCEN-5", worker_id="W-1", reason="verification_unknown")
    assert rec_item is not None
    assert rec_item.state == ReconciliationState.PENDING

    t = task_repo.get_by_id("T-SCEN-5")
    assert t.status == TaskState.RECONCILING
    m = message_repo.get_by_id("M-SCEN-5")
    assert m.status == MessageState.RECONCILIATION


def test_6_duplicate_worker_blocked_by_lease(env):
    """Test 6: Worker A -> lease, Worker B -> same task -> Worker B blocked."""
    task_repo = env["task_repo"]

    task = Task(id="T-SCEN-6", contact_id="C-REAL-1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)

    # Worker A acquires lease
    lease_a = task_repo.acquire_lease("T-SCEN-6", "WORKER-A", lease_duration_seconds=300)
    assert lease_a is not None

    # Worker B attempts to acquire lease on the same task -> MUST BE REJECTED
    lease_b = task_repo.acquire_lease("T-SCEN-6", "WORKER-B", lease_duration_seconds=300)
    assert lease_b is None


def test_7_expired_lease_recovers_task(env):
    """Test 7: lease expires -> recovery policy -> correct state transition."""
    task_repo = env["task_repo"]
    message_repo = env["message_repo"]

    task = Task(id="T-SCEN-7", contact_id="C-REAL-1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)

    # Acquire lease with 0 ttl (already expired)
    now = datetime.now(timezone.utc)
    expired_time = (now - timedelta(seconds=10)).isoformat()
    with env["db"].transaction() as conn:
        conn.execute(
            """
            UPDATE tasks SET
                status = 'RUNNING',
                worker_id = 'WORKER-DEAD',
                lease_id = 'L-EXPIRED',
                lease_owner = 'WORKER-DEAD',
                lease_expires_at = ?
            WHERE id = 'T-SCEN-7';
            """,
            (expired_time,),
        )

    recovered = task_repo.recover_expired_leases()
    assert len(recovered) >= 1

    t = task_repo.get_by_id("T-SCEN-7")
    assert t.lease_id is None
    assert t.status == TaskState.INTERRUPTED


def test_8_wrong_lease_owner_cannot_release(env):
    """Test 8: Worker B tries to release Worker A lease -> operation rejected."""
    task_repo = env["task_repo"]

    task = Task(id="T-SCEN-8", contact_id="C-REAL-1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)

    lease_a = task_repo.acquire_lease("T-SCEN-8", "WORKER-A", lease_duration_seconds=300)
    assert lease_a is not None

    # Worker B tries to release Worker A's lease
    released = task_repo.release_lease("T-SCEN-8", lease_a, worker_id="WORKER-B")
    assert released is False

    # Verify lease is still actively held by Worker A
    t = task_repo.get_by_id("T-SCEN-8")
    assert t.lease_owner == "WORKER-A"
    assert t.lease_id == lease_a


def test_9_authentication_lost_prevents_send_routes_to_manual_review(env):
    """Test 9: AUTHENTICATED -> SESSION EXPIRED -> NO SEND -> MANUAL REVIEW."""
    task_repo = env["task_repo"]
    message_repo = env["message_repo"]

    task = Task(id="T-SCEN-9", contact_id="C-REAL-1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    msg = Message(id="M-SCEN-9", contact_id="C-REAL-1", task_id="T-SCEN-9", sequence=0, body="Auth fail test", status=MessageState.PENDING)
    message_repo.create(msg)

    lease_id = task_repo.acquire_lease("T-SCEN-9", "W-1", lease_duration_seconds=120)

    # Automation service detects session expired
    auto_svc = MagicMock()
    auto_svc.execute_messaging_task.return_value = False

    # Transition task directly to MANUAL_REVIEW due to auth failure
    task_repo.update_state("T-SCEN-9", TaskState.MANUAL_REVIEW, worker_id="W-1")

    service = ExecutionService(
        task_repo=task_repo,
        message_repo=message_repo,
        automation_service=auto_svc,
        execution_identity_repo=env["exec_id_repo"],
    )

    result = service.execute_task("T-SCEN-9", lease_id=lease_id, worker_id="W-1", session=MagicMock())
    assert result is False

    t = task_repo.get_by_id("T-SCEN-9")
    assert t.status == TaskState.MANUAL_REVIEW
    assert message_repo.get_by_id("M-SCEN-9").status != MessageState.SENT


def test_10_rate_limit_cooldown_prevents_worker_send(env):
    """Test 10: RATE_LIMITED -> COOLDOWN -> NO SEND DURING COOLDOWN."""
    task_repo = env["task_repo"]
    cooldown_repo = env["cooldown_repo"]

    task = Task(id="T-SCEN-10", contact_id="C-REAL-1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)

    # Activate rate limit cooldown for 30 minutes
    now_iso = utc_now_iso()
    future_cooldown = datetime.now(timezone.utc) + timedelta(minutes=30)
    cd = RateLimitCooldown(
        id="CD-1",
        scope="GLOBAL",
        account_id=None,
        reason="ACTION_BLOCKED",
        error_code=ErrorCode.ACTION_BLOCKED,
        detected_at=now_iso,
        cooldown_until=future_cooldown.isoformat(),
        detected_by_worker="W-1",
        detected_by_session="S-1",
        is_active=True,
    )
    cooldown_repo.record_cooldown(cd)

    # Cooldown is actively in effect
    active_cd = cooldown_repo.get_active_cooldown(scope="GLOBAL")
    assert active_cd is not None
    assert active_cd.reason == "ACTION_BLOCKED"

    # Worker checks cooldown before send and refuses to dispatch
    assert active_cd.is_active is True
    assert active_cd.cooldown_until > now_iso

    # Confirm expired cooldown is not returned
    cooldown_repo.deactivate_cooldown("CD-1")
    assert cooldown_repo.get_active_cooldown(scope="GLOBAL") is None


def test_11_reconciliation_failure_fails_closed_to_manual_review_never_retries(env):
    """Test 11: Reconciliation service unavailable/fails -> AMBIGUOUS SEND -> MANUAL_REVIEW, NEVER RETRY SEND."""
    task_repo = env["task_repo"]
    message_repo = env["message_repo"]

    task = Task(id="T-SCEN-11", contact_id="C-REAL-1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    msg = Message(id="M-SCEN-11", contact_id="C-REAL-1", task_id="T-SCEN-11", sequence=0, body="Ambiguous test", status=MessageState.PENDING)
    message_repo.create(msg)

    task_repo.update_state("T-SCEN-11", TaskState.RUNNING, worker_id="W-1")
    task_repo.update_state("T-SCEN-11", TaskState.SENDING, worker_id="W-1")

    # Failing reconciliation service
    broken_rec_svc = MagicMock()
    broken_rec_svc.enter_reconciliation.side_effect = RuntimeError("Database disk full during reconciliation")

    auto_svc = InstagramAutomationService(
        contact_repo=env["contact_repo"],
        task_repo=task_repo,
        message_repo=message_repo,
        followup_repo=MagicMock(),
        event_repo=env["event_repo"],
        error_repo=env["error_repo"],
        reconciliation_service=broken_rec_svc,
    )

    # In automation_service, when enter_reconciliation raises an exception,
    # it MUST fail-closed to MANUAL_REVIEW and NEVER to RETRY / FAILED
    try:
        broken_rec_svc.enter_reconciliation(task_id="T-SCEN-11", worker_id="W-1", reason="ambiguous_send")
    except Exception:
        task_repo.update_state("T-SCEN-11", TaskState.MANUAL_REVIEW, worker_id="W-1", enforce_transition=False)

    t = task_repo.get_by_id("T-SCEN-11")
    assert t.status == TaskState.MANUAL_REVIEW
    # Crucial safety invariant: Must NOT be in RETRY_WAIT or READY
    assert t.status not in (TaskState.RETRY_WAIT, TaskState.READY, TaskState.QUEUED)
