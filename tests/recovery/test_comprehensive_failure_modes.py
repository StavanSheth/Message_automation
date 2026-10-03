"""Authoritative Failure Mode & Safety Gate Tests (Section 29).

Validates that every operational failure produces:
- deterministic error / exception
- proper state transition
- audit event recording
- safe recovery or manual review routing (no fake success, no duplicate send)
"""

import os
import pytest

from backend.bootstrap import build_production_app
from backend.config.settings import AppSettings
from backend.browser.browser_types import BrowserLaunchConfig, BrowserType, SessionStatus
from backend.browser.driver import PlaywrightBrowserDriver
from backend.browser.session import BrowserSessionInstance
from backend.browser.exceptions import BrowserLaunchError, BrowserCrashError
from backend.domain.models import Contact, Task, Account, ExecutionIdentity, utc_now_iso
from backend.domain.enums import (
    TaskState,
    TaskType,
    MessageState,
    VerificationDecision,
    SessionAuthState,
    AccountStatus,
    ErrorCode,
)
from backend.domain.errors import DuplicateTaskError
from backend.vision.ocr import VisionPipeline, StandardVisionService
from backend.events.correlation import generate_id


@pytest.fixture
def test_app(tmp_path):
    db_path = str(tmp_path / "failure_test.db")
    profiles_dir = str(tmp_path / "failure_profiles")
    settings = AppSettings(
        database_path=db_path,
        browser_profile_directory=profiles_dir,
        browser_headless=True,
        worker_mode="SINGLE_BROWSER",
        max_workers=1,
    )
    app = build_production_app(db_path=db_path, settings=settings)
    app.start()
    yield app
    app.stop()


def test_failure_browser_launch_invalid_executable(tmp_path):
    """Failure 1: Browser fails to launch with missing executable -> BROWSER_EXECUTABLE_NOT_FOUND."""
    cfg = BrowserLaunchConfig(
        browser_type=BrowserType.CHROMIUM,
        headless=True,
        executable_path=str(tmp_path / "non_existent_chrome.exe"),
    )
    driver = PlaywrightBrowserDriver(cfg)
    with pytest.raises(BrowserLaunchError) as exc_info:
        driver.launch()
    assert "BROWSER_EXECUTABLE_NOT_FOUND" in str(exc_info.value)


def test_failure_browser_session_startup_crash(tmp_path):
    """Failure 2 & 3: Session startup crash transitions STARTING -> RECOVERING -> CRASHED."""
    cfg = BrowserLaunchConfig(
        browser_type=BrowserType.CHROMIUM,
        headless=True,
        executable_path=str(tmp_path / "bad_binary.exe"),
    )
    sess = BrowserSessionInstance(
        session_id="SESS-FAIL-01",
        config=cfg,
    )
    with pytest.raises(Exception):
        sess.start()
    assert sess.status == SessionStatus.CRASHED
    assert "Session startup failed" in (sess.last_error or "")


def test_failure_browser_runtime_crashes():
    """Failure 4: Runtime crash handlers record reason and transition state to RECOVERING."""
    sess = BrowserSessionInstance(session_id="SESS-CRASH-01")
    sess.status = SessionStatus.READY

    sess.handle_browser_crash("Chrome disconnected unexpectedly")
    assert sess.status == SessionStatus.RECOVERING
    assert "Chrome disconnected" in (sess.last_error or "")

    sess.handle_page_crash("Active tab crashed")
    assert sess.status == SessionStatus.RECOVERING

    sess.handle_network_failure("DNS resolution failure for instagram.com")
    assert sess.status == SessionStatus.RECOVERING
    assert "DNS resolution" in (sess.last_error or "")


def test_failure_instagram_auth_states():
    """Failure 5: Instagram auth validator identifies LOGIN_REQUIRED, CHALLENGE, and SESSION_EXPIRED."""
    from backend.browser.instagram.auth_validator import InstagramAuthValidator
    validator = InstagramAuthValidator()

    class MockSession:
        def is_alive(self):
            return True

        def evaluate(self, script):
            # Simulate challenge page
            return {"state": "CHALLENGE", "reason": "security_checkpoint"}

    state, reason = validator.check_auth_state(MockSession())
    assert state == SessionAuthState.CHALLENGE
    assert reason == "security_checkpoint"

    class MockLoginSession:
        def is_alive(self):
            return True

        def evaluate(self, script):
            return {"state": "LOGIN_REQUIRED", "reason": "login_form_present"}

    state, reason = validator.check_auth_state(MockLoginSession())
    assert state == SessionAuthState.LOGIN_REQUIRED


def test_failure_profile_verification_mismatch_and_not_found(test_app):
    """Failure 6 & 7: Profile verifier blocks MISMATCH and NOT_FOUND from sending."""
    contact = Contact(
        id=generate_id("CNT"),
        username="target_user",
        name="Target Brand",
        instagram_url="https://www.instagram.com/target_user/",
        expected_followers=5000,
    )

    # 1. Profile missing / 404
    missing_observed = {
        "url": "https://www.instagram.com/target_user/",
        "page_missing": True,
    }
    dec, conf, signals, res = test_app.automation_service.verifier.verify_profile(contact, missing_observed)
    assert dec == VerificationDecision.NOT_FOUND
    assert test_app.automation_service.verifier.is_send_allowed(dec) is False

    # 2. Profile mismatch
    mismatch_observed = {
        "url": "https://www.instagram.com/completely_different_user/",
        "username": "completely_different_user",
        "display_name": "Totally Unrelated",
        "follower_count": 10,
        "page_missing": False,
    }
    dec, conf, signals, res = test_app.automation_service.verifier.verify_profile(contact, mismatch_observed)
    assert dec in (VerificationDecision.MISMATCH, VerificationDecision.LOW_CONFIDENCE)
    assert test_app.automation_service.verifier.is_send_allowed(dec, execution_mode="AUTOMATIC") is False


def test_failure_ocr_low_confidence_gating():
    """Failure 8: OCR engine returns UNKNOWN with 0.0 confidence when quality is below threshold."""
    pipeline = VisionPipeline(confidence_threshold=0.85)

    # Missing image
    res = pipeline.process(image_path="non_existent_screenshot.png")
    assert res.status == "UNKNOWN"
    assert res.confidence == 0.0

    # Low confidence mock engine
    def mock_low_conf_engine(img):
        return "Some text", 0.45

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        # Write minimal valid PNG header
        f.write(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\n\x00\x00\x00\n\x08\x02"
            b"\x00\x00\x00\x02PX\xea\x00\x00\x00\x16IDATx\x9cc\xfc\xff\xff?\x03n\xc0"
            b"\x84G\x8ea\xe4J\x03\x00\xa5\xe3\x03\x11\xc7z\x1cU\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        tmp_img = f.name

    try:
        res = pipeline.process(image_path=tmp_img, ocr_engine=mock_low_conf_engine)
        assert res.status == "UNKNOWN"
        assert res.confidence < 0.85
        assert "low_confidence" in res.reason
    finally:
        if os.path.exists(tmp_img):
            os.remove(tmp_img)


def test_failure_send_verifier_routes_unknown_to_reconciliation():
    """Failure 9 & 10: Unconfirmed send result routes to RECONCILIATION and MANUAL_REVIEW."""
    from backend.browser.instagram.send_verifier import InstagramSendVerifier
    verifier = InstagramSendVerifier()

    class MockSessionNotFound:
        def is_alive(self):
            return True

        def evaluate(self, script, arg):
            return {"found": False, "snippet": None, "failure_indicator": False}

    res = verifier.verify_sent_message(MockSessionNotFound(), expected_body="Hello test")
    assert res["confirmed"] is False
    assert res["message_state"] == MessageState.RECONCILIATION
    assert res["task_state"] == TaskState.MANUAL_REVIEW
    assert res["reason"] == "message_text_not_found_in_thread"


def test_failure_duplicate_task_prevention(test_app):
    """Failure 11: TaskRepository raises DuplicateTaskError on duplicate task creation."""
    account = Account(id="acc_dup", username="operator_dup", status=AccountStatus.ACTIVE)
    test_app.account_repo.create(account)

    contact = Contact(
        id=generate_id("CNT"),
        username="dup_target",
        name="Dup Target",
        instagram_url="https://www.instagram.com/dup_target/",
    )
    test_app.contact_repo.create(contact)

    task1 = Task(
        id=generate_id("TSK"),
        contact_id=contact.id,
        account_id=account.id,
        type=TaskType.MESSAGE,
        sequence=0,
        status=TaskState.READY,
    )
    test_app.task_repo.create(task1)

    task2 = Task(
        id=generate_id("TSK"),
        contact_id=contact.id,
        account_id=account.id,
        type=TaskType.MESSAGE,
        sequence=0,
        status=TaskState.READY,
    )
    with pytest.raises(DuplicateTaskError):
        test_app.task_repo.create(task2)


def test_failure_duplicate_execution_identity(test_app):
    """Failure 12: ExecutionIdentityRepository enforces execution_key uniqueness."""
    account = Account(id="acc_exec", username="operator_exec", status=AccountStatus.ACTIVE)
    test_app.account_repo.create(account)

    contact = Contact(
        id=generate_id("CNT"),
        username="exec_target",
        name="Exec Target",
        instagram_url="https://www.instagram.com/exec_target/",
    )
    test_app.contact_repo.create(contact)

    task = Task(
        id=generate_id("TSK"),
        contact_id=contact.id,
        account_id=account.id,
        type=TaskType.MESSAGE,
        status=TaskState.READY,
    )
    test_app.task_repo.create(task)

    key = "EXEC-KEY-UNIQUE-123"
    ident1 = ExecutionIdentity(
        execution_key=key,
        task_id=task.id,
        contact_id=contact.id,
        message_hash="hash123",
        worker_id="WKR-01",
        session_id="SESS-01",
    )
    test_app.execution_identity_repo.create(ident1)

    # Attempt recording duplicate execution key
    ident2 = ExecutionIdentity(
        execution_key=key,
        task_id=task.id,
        contact_id=contact.id,
        message_hash="hash456",
        worker_id="WKR-02",
        session_id="SESS-02",
    )
    with pytest.raises(Exception):
        test_app.execution_identity_repo.create(ident2)
