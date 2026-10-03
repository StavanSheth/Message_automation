"""E2E test verifying real browser profile reading and deterministic identity verification."""

import os
import pytest
from backend.browser.driver import PlaywrightBrowserDriver
from backend.browser.browser_types import BrowserLaunchConfig, BrowserType
from backend.browser.runtime import BrowserRuntimeValidator
from backend.browser.instagram.profile_verifier import InstagramProfileVerifier
from backend.browser.instagram.profile_reader import InstagramProfileReader
from backend.domain.models import Contact
from backend.domain.enums import VerificationDecision


@pytest.fixture(scope="module")
def browser_driver():
    diag = BrowserRuntimeValidator.validate_runtime()
    if not diag.can_launch:
        pytest.skip(f"Browser unavailable: {diag.actionable_fix}")

    is_headless = os.getenv("CI", "false").lower() == "true"
    cfg = BrowserLaunchConfig(
        browser_type=BrowserType.CHROMIUM,
        headless=is_headless,
        executable_path=diag.executable_path,
    )
    driver = PlaywrightBrowserDriver(cfg)
    driver.launch()
    yield driver
    driver.close()


def test_e2e_profile_verification_matching(browser_driver):
    """Real browser navigates to simulated profile and verifies matching identity."""
    contact = Contact(
        id="cnt-e2e-1",
        name="Alice Creator",
        username="alice_creator",
        instagram_url="https://www.instagram.com/alice_creator/",
    )

    # Load simulated profile page with standard Instagram DOM elements
    page_html = (
        "data:text/html,"
        "<html><head>"
        "<meta property='og:title' content='Alice Creator (@alice_creator)' />"
        "<meta property='og:url' content='https://www.instagram.com/alice_creator/' />"
        "<title>Alice Creator (@alice_creator) • Instagram</title></head>"
        "<body>"
        "<header><section>"
        "<h2>alice_creator</h2>"
        "<ul><li><span>1,500</span> followers</li></ul>"
        "<div><span>Alice Creator</span></div>"
        "<button>Message</button>"
        "</section></header>"
        "</body></html>"
    )
    browser_driver.navigate(page_html)

    # Read profile via real browser DOM extraction
    reader = InstagramProfileReader()
    observed = reader.read_profile(browser_driver)

    verifier = InstagramProfileVerifier(threshold=0.80)
    decision, confidence, signals, res = verifier.verify_profile(contact, observed)

    assert decision in (VerificationDecision.HIGH_CONFIDENCE, VerificationDecision.MEDIUM_CONFIDENCE), f"Expected PASS, got {decision}"
    assert confidence >= 0.80, f"Expected high confidence, got {confidence}"
    assert res.contact_id == contact.id


def test_e2e_profile_verification_mismatch(browser_driver):
    """Real browser navigates to mismatching profile and rejects identity."""
    contact = Contact(
        id="cnt-e2e-2",
        name="Target VIP",
        username="target_vip",
        instagram_url="https://www.instagram.com/target_vip/",
    )

    page_html = (
        "data:text/html,"
        "<html><head>"
        "<meta property='og:title' content='Bob Smith (@bob_smith)' />"
        "<title>Bob Smith (@bob_smith) • Instagram</title></head>"
        "<body>"
        "<header><section>"
        "<h2>bob_smith</h2>"
        "</section></header>"
        "</body></html>"
    )
    browser_driver.navigate(page_html)

    reader = InstagramProfileReader()
    observed = reader.read_profile(browser_driver)

    verifier = InstagramProfileVerifier(threshold=0.80)
    decision, confidence, signals, res = verifier.verify_profile(contact, observed)

    assert decision in (VerificationDecision.MISMATCH, VerificationDecision.LOW_CONFIDENCE, VerificationDecision.NOT_FOUND)
    assert confidence < 0.80
