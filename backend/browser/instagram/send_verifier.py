"""Instagram Send Verifier for confirming post-send conversation state in the DOM."""

from typing import Dict, Any, Optional
from backend.browser.session import BrowserSessionInstance
from backend.domain.enums import MessageState, TaskState
from backend.events.logger import get_logger

logger = get_logger("instagram_send_verifier")


class InstagramSendVerifier:
    """
    Inspects the Instagram conversation DOM to verify whether the intended message
    actually appears in the conversation thread.
    FAILS CLOSED: Never assumes SENT if the outcome is ambiguous.
    """

    def verify_sent_message(
        self,
        session: BrowserSessionInstance,
        expected_body: str,
        expected_username: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Inspect the active conversation thread and check if expected_body is present.
        Returns:
        {
            "confirmed": bool,
            "message_state": MessageState,
            "task_state": TaskState,
            "found_text": Optional[str],
            "reason": str,
        }
        """
        if not session or not session.is_alive():
            return {
                "confirmed": False,
                "message_state": MessageState.RECONCILIATION,
                "task_state": TaskState.RECONCILING,
                "found_text": None,
                "reason": "session_not_alive_during_verification",
            }

        expected_clean = expected_body.strip()

        result = session.evaluate(
            """(expectedText) => {
                // Search specifically inside direct message thread container
                const threadContainer = document.querySelector(
                    'div[role="grid"], div[aria-label*="Messages" i], div[data-testid="message-container"], main div[tabindex="0"], div.x78zum5.xdt5ytf'
                ) || document;

                const messageNodes = Array.from(threadContainer.querySelectorAll(
                    'div[role="row"] div[dir="auto"], div[data-testid="message-container"] div[dir="auto"], div[style*="background-color"] div[dir="auto"], div[dir="auto"]'
                )).filter(node => {
                    // Exclude message composer input, search fields, or bio elements
                    return !node.closest('textarea, [contenteditable="true"], input, header, form');
                });

                const cleanedExpected = expectedText.trim().toLowerCase();
                let foundMatch = false;
                let matchedSnippet = null;

                for (let i = messageNodes.length - 1; i >= 0; i--) {
                    const node = messageNodes[i];
                    const text = (node.innerText || '').trim();
                    if (text && (text.toLowerCase() === cleanedExpected || text.toLowerCase().includes(cleanedExpected))) {
                        foundMatch = true;
                        matchedSnippet = text.substring(0, 100);
                        break;
                    }
                }

                // Check for send failure indicators on recent bubble
                const failureIndicator = !!document.querySelector(
                    '[aria-label*="Failed to send"], [aria-label*="Not sent"], svg[aria-label*="Alert"]'
                );

                return {
                    found: foundMatch,
                    snippet: matchedSnippet,
                    failure_indicator: failureIndicator,
                };
            }""",
            expected_clean,
        )

        if not result or not isinstance(result, dict):
            logger.warning("Send verification returned null or invalid result")
            return {
                "confirmed": False,
                "message_state": MessageState.RECONCILIATION,
                "task_state": TaskState.RECONCILING,
                "found_text": None,
                "reason": "null_verification_result",
            }

        if result.get("failure_indicator"):
            logger.error("Failure indicator found near message in conversation thread")
            return {
                "confirmed": False,
                "message_state": MessageState.FAILED,
                "task_state": TaskState.FAILED,
                "found_text": result.get("snippet"),
                "reason": "failure_indicator_in_chat",
            }

        if result.get("found"):
            logger.info("Message delivery verified in conversation thread", snippet=result.get("snippet"))
            return {
                "confirmed": True,
                "message_state": MessageState.SENT,
                "task_state": TaskState.COMPLETED,
                "found_text": result.get("snippet"),
                "reason": "verified_in_dom",
            }

        # If not confirmed, do NOT assume SENT: route to RECONCILIATION / MANUAL_REVIEW
        logger.warning("Message text not found in conversation thread; routing to reconciliation")
        return {
            "confirmed": False,
            "message_state": MessageState.RECONCILIATION,
            "task_state": TaskState.MANUAL_REVIEW,
            "found_text": None,
            "reason": "message_text_not_found_in_thread",
        }
