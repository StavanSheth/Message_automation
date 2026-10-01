"""Unit tests for BrowserSessionInstance lifecycle, isolation, and crash handling."""

import pytest
from unittest.mock import MagicMock

from backend.browser.session import BrowserSessionInstance
from backend.browser.browser_types import BrowserType, SessionStatus
from backend.browser.exceptions import BrowserCrashError, BrowserSessionError
from tests.fixtures.mock_browser import MockBrowserDriver, create_mock_session


def test_session_initial_state():
    driver = MockBrowserDriver()
    session = BrowserSessionInstance(
        session_id="SESS-001",
        driver=driver,
        worker_id="WKR-01",
        browser_type=BrowserType.CHROMIUM,
    )
    assert session.status == SessionStatus.NOT_STARTED
    assert session.worker_id == "WKR-01"
    assert session.is_alive() is False


def test_session_start_and_ready():
    session = create_mock_session("SESS-002", worker_id="WKR-02")
    session.start()
    assert session.status == SessionStatus.READY
    assert session.is_alive() is True


def test_session_navigate():
    session = create_mock_session("SESS-003")
    session.start()
    url = session.navigate("https://example.com/sheet1")
    assert url == "https://example.com/sheet1"
    assert session.current_url == "https://example.com/sheet1"
    assert session.status == SessionStatus.READY


def test_session_evaluate():
    session = create_mock_session("SESS-004")
    session.start()
    val = session.evaluate("() => 'hello'")
    assert val == "hello"


def test_session_double_stop_safety():
    session = create_mock_session("SESS-005")
    session.start()
    assert session.status == SessionStatus.READY

    session.stop()
    assert session.status == SessionStatus.STOPPED

    # Double stop should be completely safe and idempotent
    session.stop()
    assert session.status == SessionStatus.STOPPED


def test_session_operations_fail_when_stopped():
    session = create_mock_session("SESS-006")
    session.start()
    session.stop()

    with pytest.raises(BrowserCrashError):
        session.navigate("https://example.com")

    with pytest.raises(BrowserCrashError):
        session.evaluate("() => 1")


def test_session_operations_fail_when_not_started():
    driver = MockBrowserDriver()
    session = BrowserSessionInstance("SESS-007", driver=driver)

    with pytest.raises(BrowserCrashError):
        session.navigate("https://example.com")

    with pytest.raises(BrowserCrashError):
        session.evaluate("() => 1")


def test_session_restart():
    session = create_mock_session("SESS-008")
    session.start()
    session.navigate("https://example.com/sheet")
    session.restart()
    assert session.status == SessionStatus.READY
    assert session.is_alive() is True


def test_session_health_check_healthy():
    session = create_mock_session("SESS-009")
    session.start()
    health = session.health_check()
    assert health.healthy is True
    assert health.browser_connected is True
    assert health.latency_ms >= 0


def test_session_health_check_detects_disconnect():
    session = create_mock_session("SESS-010")
    session.start()
    session.driver.simulate_crash()

    health = session.health_check()
    assert health.healthy is False
    assert health.browser_connected is False
    assert session.status == SessionStatus.CRASHED
