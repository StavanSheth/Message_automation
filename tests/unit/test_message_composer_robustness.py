"""Unit test suite for Message Composer robustness: Section 26 requirements.

Validates:
- Empty message / empty template rejected
- Length limits strictly enforced
- Placeholder validation & rendering
- Unicode preserved (Japanese, Russian, Korean, etc.)
- Emoji preserved
- Hindi script preserved
- Mixed languages preserved
- Very long text boundary checks
- Missing name fallback handling
- HTML tags preserved as literal text without interpretation
- Special characters preserved
"""

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

    def test_empty_template_rejected(self, composer):
        with pytest.raises(AutomationError):
            composer.compose("")

        with pytest.raises(AutomationError):
            composer.compose("   \n\t   ")

    def test_overly_long_message_rejected(self, composer, mock_session):
        long_body = "x" * 1001
        with pytest.raises(AutomationError):
            composer.compose_message(mock_session, long_body)

        with pytest.raises(AutomationError):
            composer.compose(long_body)

    def test_template_composition_with_name_and_username(self, composer):
        rendered = composer.compose(
            "Hello {{name}} (@{{username}}), check out this update!",
            contact_name="Alice",
            contact_username="alice_dev",
        )
        assert rendered == "Hello Alice (@alice_dev), check out this update!"

    def test_template_composition_missing_name_fallback(self, composer):
        # When contact_name is None, should fall back cleanly
        rendered = composer.compose(
            "Hi {{name}}!",
            contact_name=None,
            contact_username="bob_user",
        )
        assert rendered == "Hi bob_user!"

        # When neither is provided, falls back to generic "there"
        rendered2 = composer.compose(
            "Hi {{name}}!",
            contact_name=None,
            contact_username=None,
        )
        assert rendered2 == "Hi there!"

    def test_hindi_script_preserved(self, composer, mock_session):
        hindi_text = "नमस्ते! आपका काम बहुत अच्छा है। क्या हम सहयोग कर सकते हैं? 🙏"
        rendered = composer.compose(
            "नमस्ते {{name}}! क्या हाल है?",
            contact_name="राहुल",
        )
        assert "राहुल" in rendered

        mock_session.evaluate.return_value = {"success": True, "text_entered": True, "verified_match": True}
        res = composer.compose_message(mock_session, hindi_text)
        assert res.get("success") is True

    def test_html_treated_as_literal_text(self, composer, mock_session):
        html_payload = "<script>alert('xss')</script><b>Bold message</b> &amp; 'test'"
        mock_session.evaluate.return_value = {"success": True, "text_entered": True, "verified_match": True}
        res = composer.compose_message(mock_session, html_payload)
        assert res.get("success") is True
        # Verify script tags were passed as raw literal string, not stripped
        mock_session.evaluate.assert_called_once()
        args, _ = mock_session.evaluate.call_args
        assert html_payload in args

    @pytest.mark.parametrize("payload", [
        "Hello from automated outreach!",
        "Hello! 👋 Checking out your art! ✨ 🚀 🌟 🎨",
        "Line 1\nLine 2\nLine 3 with multiple breaks",
        "Special chars: & < > \" ' % $ # @ ! * ( ) _ + = ? / \\ ` ~ ^ [ ] { }",
        "Unicode: こんにちは 세계 🌍 Привет мир",
        "Hindi mixed: नमस्ते friend! कैसे हो? 👍",
        'Quotes: "Double quotes" and \'single quotes\' in text',
        "Visit our site at https://example.com/item?id=123&ref=ig",
        "A" * 1000,  # Max allowed boundary
    ])
    def test_message_formats_preservation(self, composer, mock_session, payload):
        mock_session.evaluate.return_value = {"success": True, "text_entered": True, "verified_match": True}
        res = composer.compose_message(mock_session, payload)
        assert res.get("success") is True
        assert res.get("verified_match") is True
