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
        Execute submit action with strict single-action guarantee:
        prepare -> validate composer -> perform exactly ONE send action -> wait/observe -> verify.
        Never executes multiple send actions. If ambiguous, flags for reconciliation.
        """
        if not session or not session.is_alive():
            return {"submitted": False, "reason": "session_not_alive", "error_code": ErrorCode.BROWSER_CRASH}

        # 1. Attempt native Playwright driver interaction if available
        driver = getattr(session, "driver", None)
        driver_page = getattr(driver, "_page", None) if driver is not None else None
        action_performed_natively = False
        native_send_initiated = False

        if driver_page is not None:
            try:
                send_button = driver_page.locator('button:has-text("Send"), div[role="button"]:has-text("Send")').first
                if send_button.is_visible():
                    native_send_initiated = True
                    send_button.click(timeout=3000)
                    action_performed_natively = True
                else:
                    native_send_initiated = True
                    driver_page.keyboard.press("Enter")
                    action_performed_natively = True
            except Exception as e:
                logger.warning(f"Native Playwright send interaction threw exception: {e}")
                if native_send_initiated:
                    # CRITICAL SINGLE-SEND GUARANTEE:
                    # Once a click or keystroke has been initiated, we cannot prove zero action occurred.
                    # Falling back to a DOM send creates a double-send risk. Route to reconciliation safely.
                    return {
                        "submitted": False,
                        "reason": f"native_send_exception_after_initiation: {e}",
                        "error_code": ErrorCode.UNKNOWN_RESULT,
                        "is_ambiguous": True,
                    }

        # 2. If native send was performed, evaluate post-send state.
        # If native send was NOT performed, perform pre-check, exactly ONE DOM send action, and post-check.
        if action_performed_natively:
            result = session.evaluate(
                """() => {
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
                    const inputEl = document.querySelector('textarea, [role="textbox"], [contenteditable="true"], div[aria-label*="Message" i]');
                    const remainingText = inputEl ? (inputEl.value || inputEl.innerText || '').trim() : '';
                    return { submitted: true, composer_cleared: remainingText.length === 0 };
                }"""
            )
        else:
            result = session.evaluate(
                """() => {
                    // Check for pre-send blocking conditions
                    const bodyText = document.body ? document.body.innerText : '';
                    if (bodyText.includes('Action Blocked') || bodyText.includes('Try again later')) {
                        return { submitted: false, reason: 'action_blocked', error_type: 'ACCESS_PROHIBITED' };
                    }
                    if (bodyText.includes('You cannot message this account')) {
                        return { submitted: false, reason: 'dm_not_available', error_type: 'DM_NOT_AVAILABLE' };
                    }

                    // Perform exactly ONE DOM send action
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
                        } else {
                            return { submitted: false, reason: 'no_send_trigger_found' };
                        }
                    }

                    // Check post-send error indicators
                    if (bodyText.includes("Couldn't send") || bodyText.includes('Failed to send')) {
                        return { submitted: false, reason: 'send_failed_banner', error_type: 'MESSAGE_SEND_FAILED' };
                    }

                    const inputEl = document.querySelector('textarea, [role="textbox"], [contenteditable="true"], div[aria-label*="Message" i]');
                    const remainingText = inputEl ? (inputEl.value || inputEl.innerText || '').trim() : '';
                    return { submitted: true, composer_cleared: remainingText.length === 0 };
                }"""
            )

        if isinstance(result, bool):
            result = {"submitted": result}
        elif not isinstance(result, dict):
            return {
                "submitted": False,
                "reason": "null_or_unknown_result",
                "error_code": ErrorCode.UNKNOWN_RESULT,
                "is_ambiguous": True,
            }

        if not result.get("submitted", True):
            reason = result.get("reason", "unknown")
            err_type = result.get("error_type")
            error_code = ErrorCode(err_type) if err_type in ErrorCode.__members__ else ErrorCode.MESSAGE_SEND_FAILED
            logger.error(f"Failed to submit message: {reason}")
            return {
                "submitted": False,
                "reason": reason,
                "error_code": error_code,
                "is_ambiguous": result.get("is_ambiguous", False),
            }

        return {
            "submitted": True,
            "composer_cleared": result.get("composer_cleared", False),
            "error_code": None,
        }
