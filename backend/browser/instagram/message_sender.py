"""Instagram Message Sender for dispatching send action and detecting immediate send errors."""

from typing import Dict, Any, Optional
from backend.browser.session import BrowserSessionInstance
from backend.domain.enums import ErrorCode
from backend.events.logger import get_logger

logger = get_logger("instagram_message_sender")


class InstagramMessageSender:
    """Submits the composed message in the Instagram DM interface."""

    def submit_send(self, session: BrowserSessionInstance) -> Dict[str, Any]:
        """
        Execute submit action (click 'Send' button or Enter key) and detect immediate errors.
        """
        if not session or not session.is_alive():
            return {"submitted": False, "reason": "session_not_alive", "error_code": ErrorCode.BROWSER_CRASH}

        # 1. Attempt native Playwright driver interaction if available
        driver = getattr(session, "driver", None)
        driver_page = getattr(driver, "_page", None) if driver is not None else None
        if driver_page is not None:
            try:
                # Find send button or press enter via native driver
                send_button = driver_page.locator('button:has-text("Send"), div[role="button"]:has-text("Send")').first
                if send_button.is_visible():
                    send_button.click(timeout=3000)
                else:
                    driver_page.keyboard.press("Enter")
            except Exception as e:
                logger.debug(f"Native Playwright send interaction had exception, falling back to DOM evaluation: {e}")

        # 2. Execute DOM evaluation with composer-cleared confirmation and error banner detection
        result = session.evaluate(
            """() => {
                // Look for Send button
                const buttons = Array.from(document.querySelectorAll('button, div[role="button"]'));
                let sendBtn = null;
                for (const btn of buttons) {
                    const text = (btn.innerText || '').toLowerCase().trim();
                    const aria = (btn.getAttribute('aria-label') || '').toLowerCase().trim();
                    if (text === 'send' || aria === 'send') {
                        sendBtn = btn;
                        break;
                    }
                }

                if (sendBtn) {
                    sendBtn.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                    sendBtn.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
                    sendBtn.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                } else {
                    const active = document.activeElement || document.querySelector('textarea, [contenteditable="true"]');
                    if (active) {
                        active.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', keyCode: 13, which: 13, bubbles: true }));
                        active.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', keyCode: 13, which: 13, bubbles: true }));
                    }
                }

                // Check for immediate error popups or banners
                const bodyText = document.body ? document.body.innerText : '';
                if (bodyText.includes("Couldn't send") || bodyText.includes('Failed to send')) {
                    return { submitted: false, reason: 'send_failed_banner', error_type: 'MESSAGE_SEND_FAILED' };
                }
                if (bodyText.includes('Action Blocked') || bodyText.includes('Try again later')) {
                    return { submitted: false, reason: 'action_blocked', error_type: 'ACCESS_PROHIBITED' };
                }
                if (bodyText.includes('You cannot message this account')) {
                    return { submitted: false, reason: 'dm_not_available', error_type: 'DM_NOT_AVAILABLE' };
                }

                // Verify composer state: confirm input field was cleared / consumed
                const inputEl = document.querySelector('textarea, [role="textbox"], [contenteditable="true"], div[aria-label*="Message"]');
                const remainingText = inputEl ? (inputEl.value || inputEl.innerText || '').trim() : '';
                const composerCleared = remainingText.length === 0;

                return { submitted: true, composer_cleared: composerCleared };
            }"""
        )

        if isinstance(result, bool):
            result = {"submitted": result}
        elif not isinstance(result, dict):
            result = {"submitted": False, "reason": "null_result"}

        if not result.get("submitted"):
            reason = result.get("reason", "unknown")
            err_type = result.get("error_type")
            error_code = ErrorCode(err_type) if err_type in ErrorCode.__members__ else ErrorCode.MESSAGE_SEND_FAILED
            logger.error(f"Failed to submit message: {reason}")
            return {"submitted": False, "reason": reason, "error_code": error_code}

        return {"submitted": True, "error_code": None}
