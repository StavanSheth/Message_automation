"""Unit tests for browser driver and Playwright driver abstraction."""

import pytest
from unittest.mock import MagicMock, patch

from backend.browser.browser_types import BrowserType, BrowserLaunchConfig
from backend.browser.driver import BrowserDriver, PlaywrightBrowserDriver
from backend.browser.exceptions import (
    BrowserException,
    BrowserCrashError,
    BrowserTimeoutError,
    BrowserNavigationError,
)
from tests.fixtures.mock_browser import MockBrowserDriver


def test_mock_driver_launch_and_close():
    driver = MockBrowserDriver()
    config = BrowserLaunchConfig(browser_type=BrowserType.CHROMIUM, headless=True)
    driver.launch(config)
    assert driver.is_connected() is True

    driver.close()
    assert driver.is_connected() is False


def test_mock_driver_navigation():
    driver = MockBrowserDriver()
    config = BrowserLaunchConfig(browser_type=BrowserType.CHROMIUM, headless=True)
    driver.launch(config)

    url = driver.navigate("https://example.com/spreadsheet")
    assert url == "https://example.com/spreadsheet"
    assert driver.current_url() == "https://example.com/spreadsheet"


def test_mock_driver_evaluate():
    driver = MockBrowserDriver()
    config = BrowserLaunchConfig(browser_type=BrowserType.CHROMIUM)
    driver.launch(config)

    result = driver.evaluate("() => 42")
    assert result == 42

    result2 = driver.evaluate("([a, b]) => a + b", [10, 20])
    assert result2 == 30


def test_mock_driver_simulate_crash():
    driver = MockBrowserDriver()
    driver.launch(BrowserLaunchConfig(browser_type=BrowserType.CHROMIUM))
    assert driver.is_connected() is True

    driver.simulate_crash()
    assert driver.is_connected() is False
    with pytest.raises(BrowserCrashError):
        driver.navigate("https://example.com")


def test_playwright_driver_configuration_mapping():
    config = BrowserLaunchConfig(
        browser_type=BrowserType.CHROMIUM,
        headless=True,
        profile_directory="data/profiles/test_p",
        viewport_width=1280,
        viewport_height=800,
    )
    driver = PlaywrightBrowserDriver(config)
    assert driver.config.browser_type == BrowserType.CHROMIUM
    assert driver.config.headless is True
    assert driver.config.profile_directory == "data/profiles/test_p"
