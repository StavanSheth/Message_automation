"""Deterministic E2E test verifying visible browser launch, PID, context, navigation, screenshot, and clean shutdown."""

import os
import pytest
from backend.browser.driver import PlaywrightBrowserDriver
from backend.browser.browser_types import BrowserLaunchConfig, BrowserType
from backend.browser.runtime import BrowserRuntimeValidator


@pytest.mark.e2e
def test_visible_browser_lifecycle_and_process_management():
    """
    Validates complete lifecycle:
    1. Validate runtime availability.
    2. Launch browser with headless=False (or headless in CI).
    3. Verify process PID exists.
    4. Verify context exists.
    5. Verify page exists.
    6. Navigate to safe URL (data URL / about:blank).
    7. Verify current URL and document evaluation.
    8. Capture real screenshot.
    9. Verify alive before close.
    10. Close cleanly and verify process cleanup.
    """
    diag = BrowserRuntimeValidator.validate_runtime()
    if not diag.can_launch:
        pytest.skip(f"Browser launch unavailable in environment: {diag.actionable_fix}")

    # Test visible launch unless CI environment sets headless
    is_headless = os.getenv("CI", "false").lower() == "true"
    cfg = BrowserLaunchConfig(
        browser_type=BrowserType.CHROMIUM,
        headless=is_headless,
        executable_path=diag.executable_path,
        viewport_width=1280,
        viewport_height=800,
    )
    driver = PlaywrightBrowserDriver(cfg)

    try:
        # Launch browser
        driver.launch()

        # Verify process PID exists and is positive integer
        assert driver.pid is not None, "Browser process PID must be captured"
        assert isinstance(driver.pid, int) and driver.pid > 0, f"Invalid PID: {driver.pid}"

        # Verify context and page exist
        assert driver._context is not None, "Browser context must exist"
        assert driver._page is not None, "Browser page must exist"
        assert driver.is_connected() is True, "Driver must report connected state"
        assert driver.is_alive() is True, "Driver must report alive state"

        # Navigate to a safe test URL
        test_url = "data:text/html,<html><head><title>Production Test</title></head><body><h1>Message Automation Smoke Test</h1></body></html>"
        loaded_url = driver.navigate(test_url)
        assert "Production Test" in driver.title()
        assert loaded_url.startswith("data:text/html")
        assert driver.current_url().startswith("data:text/html")

        # Evaluate JavaScript
        heading = driver.evaluate("document.querySelector('h1').innerText")
        assert heading == "Message Automation Smoke Test"

        # Capture screenshot
        screenshot_bytes = driver.take_screenshot()
        assert len(screenshot_bytes) > 0, "Screenshot must capture valid bytes"
        assert screenshot_bytes.startswith(b"\x89PNG\r\n\x1a\n"), "Screenshot must be valid PNG"

        # Verify browser remains alive until explicit close
        assert driver.is_alive() is True
    finally:
        # Clean shutdown
        driver.close()

    # Verify process terminated cleanly
    assert driver.is_connected() is False, "Driver must report disconnected after close"
    assert driver.pid is None, "PID must be reset to None after close"
