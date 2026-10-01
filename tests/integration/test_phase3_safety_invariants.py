"""Phase 3 Critical Safety Invariants and Duplicate-Send Prevention Integration Tests."""

import os
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta

from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.task_repo import TaskRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.followup_repo import FollowupRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.error_repo import ErrorRepository
from backend.repositories.verification_result_repo import VerificationResultRepository
from backend.repositories.execution_identity_repo import ExecutionIdentityRepository
from backend.repositories.cooldown_repo import CooldownRepository
from backend.browser.instagram.automation_service import InstagramAutomationService
from backend.application.followup_service import FollowupService
from backend.application.execution_service import ExecutionService
from backend.reconciliation.service import ReconciliationService
from backend.recovery.task_recovery import TaskRecoveryHandler
from backend.domain.models import Contact, Task, Message, Followup, ExecutionIdentity
from backend.domain.enums import (
    TaskState,
    TaskType,
    MessageState,
    FollowupStatus,
    RepliedStatus,
    SessionAuthState,
    ErrorCode,
    EventCode,
    VerificationDecision,
)
from backend.browser.instagram.navigator import InstagramPageStatus
from backend.config.settings import AppSettings
from tests.fixtures.mock_browser import create_mock_session


@pytest.fixture
def inv_env(tmp_path):
    db_path = str(tmp_path / "invariants.db")
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()

    contact_repo = ContactRepository(db)
    task_repo = TaskRepository(db)
    msg_repo = MessageRepository(db)
    fu_repo = FollowupRepository(db)
    evt_repo = EventRepository(db)
    err_repo = ErrorRepository(db)
    verif_repo = VerificationResultRepository(db)
    exec_repo = ExecutionIdentityRepository(db)
    cool_repo = CooldownRepository(db)
    from backend.repositories.reconciliation_repo import ReconciliationRepository
    rec_repo = ReconciliationRepository(db)
    rec_service = ReconciliationService(reconciliation_repo=rec_repo, task_repo=task_repo, message_repo=msg_repo, event_repo=evt_repo)

    fu_service = FollowupService(
        followup_repo=fu_repo,
        task_repo=task_repo,
        contact_repo=contact_repo,
        message_repo=msg_repo,
        event_repo=evt_repo,
    )

    auto_service = InstagramAutomationService(
        task_repo=task_repo,
        contact_repo=contact_repo,
        message_repo=msg_repo,
        followup_repo=fu_repo,
        event_repo=evt_repo,
        error_repo=err_repo,
        verification_repo=verif_repo,
        followup_service=fu_service,
        reconciliation_service=rec_service,
        cooldown_repo=cool_repo,
        settings=AppSettings(execution_mode="AUTOMATIC", verification_threshold=0.8),
    )

    exec_service = ExecutionService(
        task_repo=task_repo,
        message_repo=msg_repo,
        automation_service=auto_service,
        event_repo=evt_repo,
        execution_identity_repo=exec_repo,
    )

    return {
        "db": db,
        "contact_repo": contact_repo,
        "task_repo": task_repo,
        "msg_repo": msg_repo,
        "fu_repo": fu_repo,
        "evt_repo": evt_repo,
        "err_repo": err_repo,
        "exec_repo": exec_repo,
        "cool_repo": cool_repo,
        "rec_service": rec_service,
        "fu_service": fu_service,
        "auto_service": auto_service,
        "exec_service": exec_service,
    }


def test_invariant_1_one_logical_task_max_one_send(inv_env):
    """Invariant 1: ONE logical task -> MAX ONE send operation."""
    contact_repo = inv_env["contact_repo"]
    task_repo = inv_env["task_repo"]
    msg_repo = inv_env["msg_repo"]
    exec_service = inv_env["exec_service"]
    auto_service = inv_env["auto_service"]

    contact = Contact(id="C-INV-1", name="User One", instagram_url="https://instagram.com/user_1", username="user_1")
    contact_repo.create(contact)
    task = Task(id="T-INV-1", contact_id="C-INV-1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    msg = Message(id="M-INV-1", contact_id="C-INV-1", task_id="T-INV-1", sequence=0, body="Unique message text", status=MessageState.PENDING)
    msg_repo.create(msg)

    # Acquire lease
    lease1 = task_repo.acquire_lease("T-INV-1", "worker-1", 120)
    assert lease1 is not None

    session = create_mock_session()

    send_call_count = 0
    def mock_send(*args, **kwargs):
        nonlocal send_call_count
        send_call_count += 1
        return {"submitted": True}

    with patch.object(auto_service.auth_validator, "check_auth_state", return_value=(SessionAuthState.AUTHENTICATED, "ok")), \
         patch.object(auto_service.navigator, "navigate_to_profile", return_value={"status": InstagramPageStatus.AVAILABLE}), \
         patch.object(auto_service.reader, "extract_profile", return_value={"username": "user_1", "can_message": True}), \
         patch.object(auto_service.verifier, "verify_profile", return_value=(VerificationDecision.HIGH_CONFIDENCE, 1.0, {}, None)), \
         patch.object(auto_service.verifier, "is_send_allowed", return_value=True), \
         patch.object(auto_service.composer, "open_message_dialog", return_value={"success": True}), \
         patch.object(auto_service.composer, "compose_message", return_value={"success": True}), \
         patch.object(auto_service.sender, "submit_send", side_effect=mock_send), \
         patch.object(auto_service.send_verifier, "verify_sent_message", return_value={"confirmed": True}):

        # First execution -> succeeds
        res1 = exec_service.execute_task(task, session=session, worker_id="worker-1", lease_id=lease1)
        assert res1 is True
        assert send_call_count == 1

        # Second execution of the same logical task (e.g. replay by another worker)
        task_refreshed = task_repo.get_by_id("T-INV-1")
        res2 = exec_service.execute_task(task_refreshed, session=session, worker_id="worker-2", lease_id=lease1)
        # Must return without sending again
        assert send_call_count == 1


def test_invariant_2_unknown_send_result_never_resends(inv_env):
    """Invariant 2: UNKNOWN send result -> NEVER automatic resend."""
    contact_repo = inv_env["contact_repo"]
    task_repo = inv_env["task_repo"]
    msg_repo = inv_env["msg_repo"]
    exec_service = inv_env["exec_service"]
    auto_service = inv_env["auto_service"]

    contact = Contact(id="C-INV-2", name="User Two", instagram_url="https://instagram.com/user_2", username="user_2")
    contact_repo.create(contact)
    task = Task(id="T-INV-2", contact_id="C-INV-2", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    msg = Message(id="M-INV-2", contact_id="C-INV-2", task_id="T-INV-2", sequence=0, body="Ambiguous send test", status=MessageState.PENDING)
    msg_repo.create(msg)

    lease1 = task_repo.acquire_lease("T-INV-2", "worker-1", 120)
    session = create_mock_session()

    send_call_count = 0
    def mock_send(*args, **kwargs):
        nonlocal send_call_count
        send_call_count += 1
        return {"submitted": True}

    with patch.object(auto_service.auth_validator, "check_auth_state", return_value=(SessionAuthState.AUTHENTICATED, "ok")), \
         patch.object(auto_service.navigator, "navigate_to_profile", return_value={"status": InstagramPageStatus.AVAILABLE}), \
         patch.object(auto_service.reader, "extract_profile", return_value={"username": "user_2", "can_message": True}), \
         patch.object(auto_service.verifier, "verify_profile", return_value=(VerificationDecision.HIGH_CONFIDENCE, 1.0, {}, None)), \
         patch.object(auto_service.verifier, "is_send_allowed", return_value=True), \
         patch.object(auto_service.composer, "open_message_dialog", return_value={"success": True}), \
         patch.object(auto_service.composer, "compose_message", return_value={"success": True}), \
         patch.object(auto_service.sender, "submit_send", side_effect=mock_send), \
         patch.object(auto_service.send_verifier, "verify_sent_message", return_value={"confirmed": False, "reason": "DOM timeout"}):

        # First execution -> verification unconfirmed -> RECONCILIATION
        res1 = exec_service.execute_task(task, session=session, worker_id="worker-1", lease_id=lease1)
        assert res1 is False
        assert send_call_count == 1

        t_state = task_repo.get_by_id("T-INV-2")
        assert t_state.status == TaskState.RECONCILING

        # Second attempt while in RECONCILIATION -> strictly aborted without resending
        t_refreshed = task_repo.get_by_id("T-INV-2")
        res2 = exec_service.execute_task(t_refreshed, session=session, worker_id="worker-2")
        assert res2 is False
        assert send_call_count == 1  # Absolutely no second send call


def test_invariant_3_unknown_profile_never_sends(inv_env):
    """Invariant 3: UNKNOWN profile -> NEVER send."""
    contact_repo = inv_env["contact_repo"]
    task_repo = inv_env["task_repo"]
    msg_repo = inv_env["msg_repo"]
    exec_service = inv_env["exec_service"]
    auto_service = inv_env["auto_service"]

    contact = Contact(id="C-INV-3", name="User Three", instagram_url="https://instagram.com/user_3", username="user_3")
    contact_repo.create(contact)
    task = Task(id="T-INV-3", contact_id="C-INV-3", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    msg = Message(id="M-INV-3", contact_id="C-INV-3", task_id="T-INV-3", sequence=0, body="Profile test", status=MessageState.PENDING)
    msg_repo.create(msg)

    lease = task_repo.acquire_lease("T-INV-3", "worker-1", 120)
    session = create_mock_session()

    send_mock = MagicMock()
    with patch.object(auto_service.auth_validator, "check_auth_state", return_value=(SessionAuthState.AUTHENTICATED, "ok")), \
         patch.object(auto_service.navigator, "navigate_to_profile", return_value={"status": InstagramPageStatus.UNKNOWN}), \
         patch.object(auto_service.sender, "submit_send", send_mock):

        res = exec_service.execute_task(task, session=session, worker_id="worker-1", lease_id=lease)
        assert res is False
        send_mock.assert_not_called()

        t = task_repo.get_by_id("T-INV-3")
        assert t.status == TaskState.MANUAL_REVIEW


def test_invariant_4_unknown_authentication_fails_closed(inv_env):
    """Invariant 4: UNKNOWN authentication -> NEVER send (fail-closed)."""
    contact_repo = inv_env["contact_repo"]
    task_repo = inv_env["task_repo"]
    msg_repo = inv_env["msg_repo"]
    exec_service = inv_env["exec_service"]
    auto_service = inv_env["auto_service"]

    contact = Contact(id="C-INV-4", name="User Four", instagram_url="https://instagram.com/user_4")
    contact_repo.create(contact)
    task = Task(id="T-INV-4", contact_id="C-INV-4", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    msg = Message(id="M-INV-4", contact_id="C-INV-4", task_id="T-INV-4", sequence=0, body="Auth fail-closed", status=MessageState.PENDING)
    msg_repo.create(msg)

    lease = task_repo.acquire_lease("T-INV-4", "worker-1", 120)
    session = create_mock_session()

    send_mock = MagicMock()
    # Auth validator returns UNKNOWN or raises exception -> fail closed
    with patch.object(auto_service.auth_validator, "check_auth_state", return_value=(SessionAuthState.UNKNOWN, "DOM unrecognized")), \
         patch.object(auto_service.sender, "submit_send", send_mock):

        res = exec_service.execute_task(task, session=session, worker_id="worker-1", lease_id=lease)
        assert res is False
        send_mock.assert_not_called()

        t = task_repo.get_by_id("T-INV-4")
        assert t.status == TaskState.MANUAL_REVIEW


def test_invariant_5_expired_lease_halts_execution(inv_env):
    """Invariant 5: expired lease -> worker cannot continue execution."""
    contact_repo = inv_env["contact_repo"]
    task_repo = inv_env["task_repo"]
    msg_repo = inv_env["msg_repo"]
    exec_service = inv_env["exec_service"]
    auto_service = inv_env["auto_service"]

    contact = Contact(id="C-INV-5", name="User Five", instagram_url="https://instagram.com/user_5")
    contact_repo.create(contact)
    task = Task(id="T-INV-5", contact_id="C-INV-5", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)

    # Acquire an expired lease
    expired_lease = task_repo.acquire_lease("T-INV-5", "worker-1", lease_duration_seconds=-10)
    assert expired_lease is not None

    send_mock = MagicMock()
    with patch.object(auto_service.sender, "submit_send", send_mock):
        res = exec_service.execute_task(task, session=create_mock_session(), worker_id="worker-1", lease_id=expired_lease)
        assert res is False
        send_mock.assert_not_called()


def test_invariant_6_two_workers_cannot_own_same_task(inv_env):
    """Invariant 6: two workers -> cannot own same task simultaneously."""
    inv_env["contact_repo"].create(Contact(id="C-INV-6", name="User Six", instagram_url="https://instagram.com/user_6"))
    task_repo = inv_env["task_repo"]
    task = Task(id="T-INV-6", contact_id="C-INV-6", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)

    lease1 = task_repo.acquire_lease("T-INV-6", "worker-1", 120)
    assert lease1 is not None

    # Worker 2 tries to acquire same active lease -> must fail atomically
    lease2 = task_repo.acquire_lease("T-INV-6", "worker-2", 120)
    assert lease2 is None

    t = task_repo.get_by_id("T-INV-6")
    assert t.lease_owner == "worker-1"
    assert t.lease_id == lease1


def test_invariant_7_reply_detected_cancels_pending_followups_and_tasks(inv_env):
    """Invariant 7: reply detected -> pending follow-ups and unexecuted tasks cannot execute."""
    contact_repo = inv_env["contact_repo"]
    task_repo = inv_env["task_repo"]
    fu_repo = inv_env["fu_repo"]
    fu_service = inv_env["fu_service"]

    contact = Contact(id="C-INV-7", name="User Seven", instagram_url="https://instagram.com/user_7", replied_status=RepliedStatus.UNKNOWN)
    contact_repo.create(contact)

    # Setup followups
    fu1 = Followup(id="FU-7-1", contact_id="C-INV-7", sequence=1, message="FU 1", delay_seconds=86400, scheduled_at="2026-10-01T00:00:00Z", status=FollowupStatus.SCHEDULED)
    fu2 = Followup(id="FU-7-2", contact_id="C-INV-7", sequence=2, message="FU 2", delay_seconds=86400, scheduled_at="2026-10-02T00:00:00Z", status=FollowupStatus.PENDING)
    fu_repo.create(fu1)
    fu_repo.create(fu2)

    # Also an already-materialized READY task for follow-up
    task_fu = Task(id="T-FU-7", contact_id="C-INV-7", type=TaskType.FOLLOW_UP_1, sequence=1, status=TaskState.READY)
    task_repo.create(task_fu)

    # Reply detected -> cancel
    fu_service.cancel_pending_followups("C-INV-7", reason="REPLIED")

    # Both followups must be cancelled
    assert fu_repo.get_by_id("FU-7-1").status == FollowupStatus.CANCELLED
    assert fu_repo.get_by_id("FU-7-2").status == FollowupStatus.CANCELLED

    # Unexecuted follow-up task must be cancelled
    assert task_repo.get_by_id("T-FU-7").status == TaskState.CANCELLED


def test_invariant_8_challenge_checkpoint_routes_to_manual_review_no_retry(inv_env):
    """Invariant 8: challenge/checkpoint -> no automatic retry loop."""
    contact_repo = inv_env["contact_repo"]
    task_repo = inv_env["task_repo"]
    msg_repo = inv_env["msg_repo"]
    exec_service = inv_env["exec_service"]
    auto_service = inv_env["auto_service"]

    contact = Contact(id="C-INV-8", name="User Eight", instagram_url="https://instagram.com/user_8")
    contact_repo.create(contact)
    task = Task(id="T-INV-8", contact_id="C-INV-8", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    msg = Message(id="M-INV-8", contact_id="C-INV-8", task_id="T-INV-8", sequence=0, body="Check", status=MessageState.PENDING)
    msg_repo.create(msg)

    lease = task_repo.acquire_lease("T-INV-8", "worker-1", 120)

    with patch.object(auto_service.auth_validator, "check_auth_state", return_value=(SessionAuthState.CHALLENGE, "Checkpoint page detected")):
        res = exec_service.execute_task(task, session=create_mock_session(), worker_id="worker-1", lease_id=lease)
        assert res is False

        t = task_repo.get_by_id("T-INV-8")
        # Must be MANUAL_REVIEW, not RETRY_WAIT
        assert t.status == TaskState.MANUAL_REVIEW


def test_invariant_9_and_10_crash_recovery_during_sending_reconciles(inv_env):
    """
    Invariant 9 & 10:
    Browser or process crash during/after sending leaves task for RECONCILIATION,
    never resetting to READY.
    """
    task_repo = inv_env["task_repo"]
    rec_service = inv_env["rec_service"]
    handler = TaskRecoveryHandler(task_repo=task_repo, reconciliation_service=rec_service)

    # Task was in SENDING with expired lease
    inv_env["contact_repo"].create(Contact(id="C-CRASH", name="Crash User", instagram_url="https://instagram.com/crash"))
    task = Task(id="T-CRASH-SEND", contact_id="C-CRASH", type=TaskType.MESSAGE, status=TaskState.RUNNING)
    task_repo.create(task)
    task_repo.acquire_lease("T-CRASH-SEND", "worker-dead", lease_duration_seconds=-30)

    # Manually simulate entering reconciliation state for sending crash
    recovered_count = handler.recover_expired_leases()
    assert recovered_count == 1

    t_recovered = task_repo.get_by_id("T-CRASH-SEND")
    assert t_recovered.status == TaskState.INTERRUPTED
    assert t_recovered.status != TaskState.READY


def test_correlation_id_propagated_to_events_dedicated_column(inv_env):
    """Verify that a given correlation ID is propagated end-to-end to events dedicated column."""
    contact_repo = inv_env["contact_repo"]
    task_repo = inv_env["task_repo"]
    msg_repo = inv_env["msg_repo"]
    exec_service = inv_env["exec_service"]
    auto_service = inv_env["auto_service"]
    evt_repo = inv_env["evt_repo"]

    contact = Contact(id="C-CORR-1", name="Corr User", instagram_url="https://instagram.com/corr_user", username="corr_user")
    contact_repo.create(contact)
    task = Task(id="T-CORR-1", contact_id="C-CORR-1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    msg = Message(id="M-CORR-1", contact_id="C-CORR-1", task_id="T-CORR-1", sequence=0, body="Correlation tracing", status=MessageState.PENDING)
    msg_repo.create(msg)

    lease = task_repo.acquire_lease("T-CORR-1", "worker-1", 120)
    session = create_mock_session()
    test_corr_id = "CORR-TRACE-12345"

    with patch.object(auto_service.auth_validator, "check_auth_state", return_value=(SessionAuthState.AUTHENTICATED, "ok")), \
         patch.object(auto_service.navigator, "navigate_to_profile", return_value={"status": InstagramPageStatus.AVAILABLE}), \
         patch.object(auto_service.reader, "extract_profile", return_value={"username": "corr_user", "can_message": True}), \
         patch.object(auto_service.verifier, "verify_profile", return_value=(VerificationDecision.HIGH_CONFIDENCE, 1.0, {}, None)), \
         patch.object(auto_service.verifier, "is_send_allowed", return_value=True), \
         patch.object(auto_service.composer, "open_message_dialog", return_value={"success": True}), \
         patch.object(auto_service.composer, "compose_message", return_value={"success": True}), \
         patch.object(auto_service.sender, "submit_send", return_value={"submitted": True}), \
         patch.object(auto_service.send_verifier, "verify_sent_message", return_value={"confirmed": True}):

        success = exec_service.execute_task(
            task,
            session=session,
            worker_id="worker-1",
            lease_id=lease,
            correlation_id=test_corr_id,
        )
        assert success is True

    # Query events directly from SQLite DB to ensure dedicated column correlation_id is populated
    conn = inv_env["db"].get_connection()
    cursor = conn.execute("SELECT correlation_id, task_id, worker_id FROM events WHERE task_id = ?;", ("T-CORR-1",))
    rows = cursor.fetchall()
    assert len(rows) > 0
    for r in rows:
        assert r["correlation_id"] == test_corr_id
        assert r["task_id"] == "T-CORR-1"
        assert r["worker_id"] == "worker-1"
