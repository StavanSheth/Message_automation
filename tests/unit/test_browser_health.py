"""Unit tests for BrowserHealthChecker responsive diagnostics."""

import pytest
from backend.browser.health import BrowserHealthChecker
from tests.fixtures.mock_browser import create_mock_session


def test_health_checker_healthy_session():
    session = create_mock_session("SESS-H1")
    session.start()

    checker = BrowserHealthChecker(timeout_seconds=5)
    result = checker.check(session)

    assert result.healthy is True
    assert result.browser_connected is True
    assert result.latency_ms >= 0


def test_health_checker_disconnected_session():
    session = create_mock_session("SESS-H2")
    session.start()
    session.driver.simulate_crash()

    checker = BrowserHealthChecker(timeout_seconds=5)
    result = checker.check(session)

    assert result.healthy is False
    assert result.browser_connected is False
    assert result.error_code is not None


def test_health_checker_unstarted_session():
    session = create_mock_session("SESS-H3", auto_start=False)
    checker = BrowserHealthChecker(timeout_seconds=5)
    result = checker.check(session)

    assert result.healthy is False
