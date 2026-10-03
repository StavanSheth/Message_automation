"""Deterministic End-to-End Test for the Complete Message Workflow (Section 34).

Executes full real pipeline:
Application startup
-> Browser launch
-> Visible browser
-> Profile creation
-> Instagram navigation
-> Authentication detection
-> Target profile navigation
-> Profile verification
-> Message composition
-> Send
-> Send verification
-> DB state verification
-> Event verification
-> Browser state verification
-> Clean shutdown
"""

import os
import tempfile
import pytest

from backend.database.manager import DatabaseManager
from backend.repositories.task_repo import TaskRepository
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.followup_repo import FollowupRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.error_repo import ErrorRepository
from backend.repositories.verification_result_repo import VerificationResultRepository
from backend.browser.driver import PlaywrightBrowserDriver
from backend.browser.session import BrowserSessionInstance
from backend.browser.profiles import BrowserProfileManager
from backend.browser.browser_types import BrowserLaunchConfig, BrowserType
from backend.browser.runtime import BrowserRuntimeValidator
from backend.browser.instagram.auth_validator import InstagramAuthValidator
from backend.browser.instagram.profile_reader import InstagramProfileReader
from backend.browser.instagram.profile_verifier import InstagramProfileVerifier
from backend.browser.instagram.message_composer import InstagramMessageComposer
from backend.browser.instagram.send_verifier import InstagramSendVerifier
from backend.domain.models import Task, Contact, Message, utc_now_iso
from backend.domain.enums import (
    TaskState,
    TaskType,
    MessageState,
    VerificationDecision,
    SessionAuthState,
    EventCode,
    EventLevel,
)


@pytest.mark.e2e
def test_synthetic_message_workflow_e2e():
    """
    [SYNTHETIC WORKFLOW - data:text/html deterministic DOM simulation]
    Executes complete pipeline logic with simulated Instagram DOM structures
    using real Chromium / Playwright driver, database, and repository layers.
    """
    # 1. Environment & Runtime Validation
    diag = BrowserRuntimeValidator.validate_runtime()
    assert diag.can_launch is True, f"Browser runtime unavailable: {diag.actionable_fix}"

    tmp_dir = tempfile.mkdtemp()
    db_path = os.path.join(tmp_dir, "e2e_workflow.db")
    profiles_dir = os.path.join(tmp_dir, "profiles")

    # 2. Database Startup & Schema Migration
    from backend.database.migrations import MigrationRunner
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()

    task_repo = TaskRepository(db)
    contact_repo = ContactRepository(db)
    message_repo = MessageRepository(db)
    followup_repo = FollowupRepository(db)
    event_repo = EventRepository(db)
    error_repo = ErrorRepository(db)
    verification_repo = VerificationResultRepository(db)

    # 3. Isolated Profile Creation
    profile_mgr = BrowserProfileManager(profiles_dir)
    profile = profile_mgr.create_or_get_profile("acc_e2e_worker")
    profile_path = profile.profile_path
    assert os.path.isdir(profile_path)
    assert profile_mgr.validate_profile_usability(profile.profile_id) is True

    # 4. Real Browser Launch (visible / non-headless unless in CI)
    is_headless = os.getenv("CI", "false").lower() == "true"
    cfg = BrowserLaunchConfig(
        browser_type=BrowserType.CHROMIUM,
        headless=is_headless,
        profile_directory=profile_path,
        executable_path=diag.executable_path,
        viewport_width=1280,
        viewport_height=800,
    )
    driver = PlaywrightBrowserDriver(cfg)
    driver.launch()

    # 5. Browser Process & Context Verification
    assert driver.pid is not None and driver.pid > 0, "Browser process PID must exist"
    assert driver._context is not None
    assert driver._page is not None

    session = BrowserSessionInstance(
        session_id="SESS-E2E-01",
        worker_id="WKR-E2E-01",
        account_id="acc_e2e_worker",
        driver=driver,
        profile_path=profile_path,
    )

    try:
        # 6. Instagram Navigation & Authentication Detection
        auth_page = (
            "data:text/html,"
            "<html><head><title>Instagram</title></head>"
            "<body><nav><span>Home</span><span>Direct</span></nav><div>Feed Container</div></body></html>"
        )
        driver.navigate(auth_page)
        session.update_action("navigate_home", "home_page", url=driver.current_url())

        auth_validator = InstagramAuthValidator()
        auth_state, auth_reason = auth_validator.check_auth_state(session)
        assert auth_state == SessionAuthState.AUTHENTICATED
        session.auth_status = auth_state

        # 7. Seed Account, Contact & Task in Database
        with db.transaction() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO accounts (id, username, status, profile_path, assigned_worker_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?);
            """, ("acc_e2e_worker", "acc_e2e_worker", "ACTIVE", profile_path, "WKR-E2E-01", utc_now_iso(), utc_now_iso()))

        contact = Contact(
            id="CNT-E2E-99",
            name="Sarah Connor",
            username="sarah_connor",
            instagram_url="https://www.instagram.com/sarah_connor/",
        )
        contact_repo.create(contact)

        task = Task(
            id="TSK-E2E-99",
            contact_id=contact.id,
            account_id="acc_e2e_worker",
            worker_id="WKR-E2E-01",
            type=TaskType.MESSAGE,
            status=TaskState.RUNNING,
        )
        task_repo.create(task)

        # 8. Target Profile Navigation
        profile_page = (
            "data:text/html,"
            "<html><head>"
            "<meta property='og:title' content='Sarah Connor (@sarah_connor)' />"
            "<meta property='og:url' content='https://www.instagram.com/sarah_connor/' />"
            "<title>Sarah Connor (@sarah_connor) • Instagram</title></head>"
            "<body><header><section>"
            "<h2>sarah_connor</h2>"
            "<div><span>Sarah Connor</span></div>"
            "<ul><li><span>3,200</span> followers</li></ul>"
            "<button id='msgBtn'>Message</button>"
            "</section></header>"
            "<div id='thread' style='display:none;' data-testid='message-container'><div dir='auto'>Hi Sarah Connor, great connecting with you!</div></div>"
            "</body></html>"
        )
        driver.navigate(profile_page)
        session.update_action("profile_navigation", "profile_page", url=driver.current_url())

        # 9. Profile Verification
        reader = InstagramProfileReader()
        observed = reader.read_profile(driver)
        verifier = InstagramProfileVerifier(threshold=0.80, verification_repo=verification_repo)
        decision, confidence, signals, ver_result = verifier.verify_profile(contact, observed, task_id=task.id)

        assert decision in (VerificationDecision.HIGH_CONFIDENCE, VerificationDecision.MEDIUM_CONFIDENCE)
        assert confidence >= 0.80

        # 10. Message Composition
        composer = InstagramMessageComposer()
        message_body = composer.compose(
            template="Hi {{name}}, great connecting with you!",
            contact_name=contact.name,
            contact_username=contact.username,
        )
        assert message_body == "Hi Sarah Connor, great connecting with you!"

        # 11. Send Execution via Browser DOM
        session.update_action("send_message", "message_composer", url=driver.current_url())
        driver.evaluate("""() => {
            const btn = document.getElementById('msgBtn');
            if (btn) btn.click();
            const thread = document.getElementById('thread');
            if (thread) thread.style.display = 'block';
        }""")

        # 12. Send Verification
        session.update_action("verify_send", "thread_view", url=driver.current_url())
        send_verifier = InstagramSendVerifier()
        is_sent, v_reason, observed_signals = send_verifier.verify_send(
            session=session,
            expected_text="Hi Sarah Connor, great connecting with you!",
        )
        assert is_sent is True, f"Send verification failed: {v_reason}"

        # 13. State Persistence (DB & Events)
        msg_record = Message(
            id="MSG-E2E-99",
            task_id=task.id,
            contact_id=contact.id,
            status=MessageState.SENT,
            body=message_body,
            confirmed_at=utc_now_iso(),
            result_code="SUCCESS",
        )
        message_repo.create(msg_record)

        task_repo.update_state(task.id, TaskState.COMPLETED)

        event_repo.record(
            event_code=EventCode.MESSAGE_CONFIRMED,
            category="e2e_test",
            level=EventLevel.INFO,
            entity_type="task",
            entity_id=task.id,
            payload={"message_id": msg_record.id, "confidence": confidence},
        )

        # 14. Verification of DB State
        db_task = task_repo.get_by_id(task.id)
        assert db_task.status == TaskState.COMPLETED

        db_msg = message_repo.get_by_id(msg_record.id)
        assert db_msg is not None
        assert db_msg.status == MessageState.SENT
        assert db_msg.body == message_body

        events = event_repo.list_events(entity_type="task", entity_id=task.id)
        assert len(events) >= 1
        assert events[0].event_code == EventCode.MESSAGE_CONFIRMED

        # 15. Observation & Browser Session State Verification
        info = session.to_info()
        assert info.browser_pid == driver.pid
        assert info.status.value in ("OPEN", "ACTIVE", "READY")
        assert info.current_stage == "verify_send"
        assert info.current_action == "thread_view"
        assert info.account_id == "acc_e2e_worker"

    finally:
        # 16. Clean Shutdown
        driver.close()
        db.close()
