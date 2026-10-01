"""Phase 3 unit tests for Instagram Authentication Validator (Workstream K)."""

import pytest
from unittest.mock import MagicMock
from backend.browser.instagram.auth_validator import InstagramAuthValidator
from backend.browser.session import BrowserSessionInstance
from backend.domain.enums import SessionAuthState


def test_auth_validator_session_dead():
    validator = InstagramAuthValidator()
    mock_session = MagicMock(spec=BrowserSessionInstance)
    mock_session.is_alive.return_value = False

    state, reason = validator.check_auth_state(mock_session)
    assert state == SessionAuthState.UNKNOWN
    assert reason == "session_not_alive"


def test_auth_validator_login_required():
    validator = InstagramAuthValidator()
    mock_session = MagicMock(spec=BrowserSessionInstance)
    mock_session.is_alive.return_value = True
    mock_session.evaluate.return_value = {"state": "LOGIN_REQUIRED", "reason": "login_form_present"}

    state, reason = validator.check_auth_state(mock_session)
    assert state == SessionAuthState.LOGIN_REQUIRED
    assert "login" in reason


def test_auth_validator_challenge():
    validator = InstagramAuthValidator()
    mock_session = MagicMock(spec=BrowserSessionInstance)
    mock_session.is_alive.return_value = True
    mock_session.evaluate.return_value = {"state": "CHALLENGE", "reason": "security_challenge"}

    state, reason = validator.check_auth_state(mock_session)
    assert state == SessionAuthState.CHALLENGE
    assert "challenge" in reason


def test_auth_validator_authenticated():
    validator = InstagramAuthValidator()
    mock_session = MagicMock(spec=BrowserSessionInstance)
    mock_session.is_alive.return_value = True
    mock_session.evaluate.return_value = {"state": "AUTHENTICATED", "reason": "navigation_elements_present"}

    state, reason = validator.check_auth_state(mock_session)
    assert state == SessionAuthState.AUTHENTICATED
    assert "navigation" in reason
