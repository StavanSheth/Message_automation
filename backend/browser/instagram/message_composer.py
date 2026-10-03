"""Instagram Message Composer for opening dialog and entering message text."""

from typing import Dict, Any, Optional
from backend.browser.session import BrowserSessionInstance
from backend.browser.exceptions import BrowserCrashError, BrowserTimeoutError
from backend.domain.enums import ErrorCode
from backend.domain.errors import AutomationError
from backend.events.logger import get_logger

logger = get_logger("instagram_message_composer")


class InstagramMessageComposer:
    """Interacts with Instagram browser DOM to open DM interface and enter message text."""

    def open_message_dialog(self, session: BrowserSessionInstance, timeout_ms: int = 15000) -> Dict[str, Any]:
        """
        Locate and click the 'Message' action button on target profile to open DM conversation.
        """
        if not session or not session.is_alive():
            return {"success": False, "reason": "session_not_alive"}

        result = session.evaluate(
            """() => {
                // Find Message button in profile header
                const buttons = Array.from(document.querySelectorAll('header button, header div[role="button"]'));
                let messageBtn = null;
                for (const btn of buttons) {
                    const text = (btn.innerText || '').toLowerCase().trim();
                    if (text === 'message' || text.includes('message')) {
                        messageBtn = btn;
                        break;
                    }
                }

                if (!messageBtn) {
                    // Check if DM not allowed / button missing
                    return { success: false, reason: 'message_button_not_found' };
                }

                // Click the Message button
                messageBtn.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                messageBtn.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
                messageBtn.dispatchEvent(new MouseEvent('click', { bubbles: true }));

                return { success: true };
            }"""
        )

        if isinstance(result, bool):
            result = {"success": result}
        elif not isinstance(result, dict):
            result = {"success": False, "reason": "null_result"}

        if not result.get("success"):
            reason = result.get("reason", "unknown")
            logger.warning(f"Could not open message dialog: {reason}")
            return {"success": False, "reason": reason}

        # Wait for message input area to appear or detect recipient DM blocking
        ready = session.evaluate(
            """() => {
                const bodyText = document.body ? document.body.innerText : '';
                if (
                    bodyText.includes("can't message this account") ||
                    bodyText.includes("cannot message this account") ||
                    bodyText.includes("doesn't allow new message requests") ||
                    bodyText.includes("don't allow new message requests") ||
                    bodyText.includes("Not everyone can message this account") ||
                    bodyText.includes("can't receive your message") ||
                    bodyText.includes("cannot receive your message")
                ) {
                    return { blocked: true, reason: 'dm_blocked_by_recipient' };
                }
                const input = document.querySelector(
                    'textarea, [role="textbox"], [contenteditable="true"], div[aria-label*="Message" i]'
                );
                return { blocked: false, ready: !!input };
            }"""
        )
        if isinstance(ready, dict):
            if ready.get("blocked"):
                return {"success": False, "blocked": True, "reason": ready.get("reason", "dm_blocked_by_recipient")}
            return {"success": True, "dialog_open": bool(ready.get("ready"))}
        return {"success": True, "dialog_open": bool(ready)}

    def compose_message(self, session: BrowserSessionInstance, body: str) -> Dict[str, Any]:
        """
        Type the configured message content into the active message input field.
        """
        if not session or not session.is_alive():
            return {"success": False, "reason": "session_not_alive"}

        if not body or not isinstance(body, str):
            raise AutomationError(code=ErrorCode.INVALID_DATA, message="Message body cannot be empty")

        result = session.evaluate(
            """(messageText) => {
                const input = document.querySelector(
                    'textarea, [role="textbox"], [contenteditable="true"], div[aria-label*="Message"]'
                );

                if (!input) {
                    return { success: false, reason: 'input_field_not_found' };
                }

                input.focus();

                if (input.tagName === 'TEXTAREA' || input.tagName === 'INPUT') {
                    input.value = messageText;
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                } else {
                    // Contenteditable div
                    input.innerText = messageText;
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                }

                return { success: true, text_entered: true };
            }""",
            body,
        )

        if isinstance(result, bool):
            result = {"success": result}
        elif not isinstance(result, dict):
            result = {"success": False, "reason": "null_result"}

        if not result.get("success"):
            reason = result.get("reason", "unknown")
            logger.error(f"Failed to compose message: {reason}")
            return {"success": False, "reason": reason}

        return {"success": True}
