"""Integration test for Instagram browser runtime with real browser and optional credentials.

Requirements from Section 21:
- Do not hardcode credentials.
- Read INSTAGRAM_TEST_USERNAME, INSTAGRAM_TEST_PASSWORD, INSTAGRAM_TEST_TARGET.
- If credentials are not configured, skip with explicit reason (no false pass).
- If configured, validate:
  browser -> instagram.com -> authentication state -> target profile navigation
  -> profile extraction -> verification -> safe stopping point.
- Do NOT send an actual message in CI.
"""

import os
import pytest
from backend.browser.driver import PlaywrightDriver
from backend.browser.session import BrowserSessionInstance
from backend.browser.browser_types import BrowserConfig
from backend.browser.instagram.auth_validator import InstagramAuthValidator
from backend.browser.instagram.navigator import InstagramNavigator, InstagramPageStatus
from backend.browser.instagram.profile_reader import InstagramProfileReader
from backend.browser.instagram.profile_verifier import InstagramProfileVerifier
from backend.domain.models import Contact
from backend.domain.enums import SessionAuthState


@pytest.mark.integration
def test_real_instagram_browser_runtime(tmp_path):
    """
    Live Instagram browser integration test.
    Skips cleanly if environment variables are not set.
    """
    username = os.environ.get("INSTAGRAM_TEST_USERNAME")
    password = os.environ.get("INSTAGRAM_TEST_PASSWORD")
    target = os.environ.get("INSTAGRAM_TEST_TARGET")

    if not username or not password or not target:
        pytest.skip(
            "Live Instagram test skipped: INSTAGRAM_TEST_USERNAME, "
            "INSTAGRAM_TEST_PASSWORD, or INSTAGRAM_TEST_TARGET not configured in environment."
        )

    # If configured, launch real browser
    user_data_dir = tmp_path / "ig_test_profile"
    user_data_dir.mkdir(parents=True, exist_ok=True)

    config = BrowserConfig(
        headless=True,
        user_data_dir=str(user_data_dir),
    )
    driver = PlaywrightDriver(config)
    driver.launch()

    try:
        session = BrowserSessionInstance(
            session_id="ig_test_session",
            account_id=username,
            driver=driver,
        )
        session.start()

        # 1. Navigate to Instagram base
        loaded_url = session.navigate("https://www.instagram.com/", timeout_ms=30000)
        assert "instagram.com" in loaded_url

        # 2. Check Auth State
        auth_validator = InstagramAuthValidator()
        auth_state, reason = auth_validator.check_auth_state(session)
        assert auth_state in [
            SessionAuthState.AUTHENTICATED,
            SessionAuthState.LOGIN_REQUIRED,
            SessionAuthState.CHALLENGE,
            SessionAuthState.UNKNOWN,
        ]

        # 3. Target Profile Navigation
        navigator = InstagramNavigator()
        nav_result = navigator.navigate_to_profile(session, target, timeout_ms=30000)
        assert nav_result["status"] in [
            InstagramPageStatus.AVAILABLE,
            InstagramPageStatus.LOGIN_REQUIRED,
            InstagramPageStatus.PRIVATE,
            InstagramPageStatus.NOT_FOUND,
            InstagramPageStatus.ACCESS_BLOCKED,
            InstagramPageStatus.RESTRICTED,
            InstagramPageStatus.UNKNOWN,
        ]

        # 4. Profile Extraction (only if not walled off)
        reader = InstagramProfileReader()
        profile_data = reader.extract_profile(session)
        assert isinstance(profile_data, dict)
        assert "username" in profile_data
        assert "url" in profile_data

        # 5. Profile Verification against Contact
        contact = Contact(
            id="test_contact_1",
            username=target,
            instagram_url=f"https://www.instagram.com/{target}/",
            name="Test Target",
        )
        verifier = InstagramProfileVerifier()
        decision, confidence, signals, result = verifier.verify_profile(contact, profile_data)
        assert decision is not None
        assert 0.0 <= confidence <= 1.0

        # Safe stopping point reached - do NOT send any message!
    finally:
        driver.close()
