"""Unit test suite for Message Composer robustness: Section 17 requirements."""

import pytest
from unittest.mock import MagicMock
from backend.browser.instagram.message_composer import InstagramMessageComposer
from backend.browser.session import BrowserSessionInstance
from backend.domain.errors import AutomationError


class TestMessageComposerRobustness:
    @pytest.fixture
    def mock_session(self):
        sess = MagicMock(spec=BrowserSessionInstance)
        sess.is_alive.return_value = True
        return sess

    @pytest.fixture
    def composer(self):
        return InstagramMessageComposer()

    def test_empty_message_rejected(self, composer, mock_session):
        with pytest.raises(AutomationError):
            composer.compose_message(mock_session, "")

        with pytest.raises(AutomationError):
            composer.compose_message(mock_session, "   \n\t   ")

    def test_overly_long_message_rejected(self, composer, mock_session):
        long_body = "x" * 1001
        with pytest.raises(AutomationError):
            composer.compose_message(mock_session, long_body)

    @pytest.mark.parametrize("payload", [
        "Hello from automated outreach!",
        "Hello! 👋 Checking out your art! ✨ 🚀",
        "Line 1\nLine 2\nLine 3 with multiple breaks",
        "Special chars: & < > \" ' % $ # @ ! * ( ) _ + = ? / \\",
        "Unicode: こんにちは 세계 🌍 Привет",
        'Quotes: "Double quotes" and \'single quotes\' in text',
        "Visit our site at https://example.com/item?id=123&ref=ig",
        "A" * 999,  # Longest valid message
    ])
    def test_message_formats_preservation(self, composer, mock_session, payload):
        mock_session.evaluate.return_value = {"success": True, "text_entered": True, "verified_match": True}
        res = composer.compose_message(mock_session, payload)
        assert res.get("success") is True
        assert res.get("verified_match") is True
        mock_session.evaluate.assert_called_once()
