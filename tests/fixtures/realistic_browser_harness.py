"""Realistic stateful browser driver harness for Phase 4 E2E testing."""

from typing import Optional, Any, List, Dict
from urllib.parse import urlparse
from backend.browser.driver import BrowserDriver
from backend.browser.browser_types import BrowserLaunchConfig, SessionStatus
from backend.browser.session import BrowserSessionInstance


class RealisticBrowserDriverHarness(BrowserDriver):
    """
    Stateful, deterministic browser driver harness.
    Implements BrowserDriver interface and realistically models the browser DOM lifecycle:
    navigation, authentication validation, profile detection, DM dialog opening,
    message composing, single send execution, and message thread verification.
    """

    def __init__(self):
        self._launched = False
        self._connected = False
        self._current_url = ""
        self._current_username = ""
        self._page_type = "AUTHENTICATED"
        self._dialog_open = False
        self._composed_text = ""
        self._sent_messages: List[str] = []
        self.navigate_history: List[str] = []

    def launch(self, config: Optional[BrowserLaunchConfig] = None) -> None:
        self._launched = True
        self._connected = True

    def close(self) -> None:
        self._launched = False
        self._connected = False
        self._dialog_open = False

    def is_connected(self) -> bool:
        return self._connected

    def new_context(self, profile_path: Optional[str] = None) -> None:
        pass

    def close_context(self) -> None:
        pass

    def new_page(self) -> None:
        pass

    def close_page(self) -> None:
        pass

    def navigate(self, url: str, timeout_ms: Optional[int] = None) -> str:
        if not self._connected:
            from backend.browser.exceptions import BrowserCrashError
            raise BrowserCrashError("Browser disconnected")

        self._current_url = url
        self.navigate_history.append(url)

        # Detect page type from URL
        if "/challenge/" in url or "/checkpoint/" in url:
            self._page_type = "CHALLENGE"
        elif "/accounts/login" in url:
            self._page_type = "LOGIN"
        elif "/direct/inbox" in url:
            self._page_type = "INBOX"
        else:
            self._page_type = "PROFILE"
            parsed = urlparse(url)
            parts = [p for p in parsed.path.strip("/").split("/") if p]
            self._current_username = parts[0] if parts else "user"

        return url

    def current_url(self) -> str:
        return self._current_url

    def wait_for_load(self, state: str = "load", timeout_ms: Optional[int] = None) -> None:
        pass

    def evaluate(self, expression: str, arg: Any = None) -> Any:
        if not self._connected:
            from backend.browser.exceptions import BrowserCrashError
            raise BrowserCrashError("Browser disconnected")

        # 1. Instagram Auth Validator check
        if "hasProfileNav" in expression or "hasNav && hasProfileNav" in expression:
            if self._page_type == "CHALLENGE":
                return {"state": "CHALLENGE", "reason": "security_challenge_or_checkpoint"}
            if self._page_type == "LOGIN":
                return {"state": "LOGIN_REQUIRED", "reason": "login_form_present"}
            return {"state": "AUTHENTICATED", "reason": "navigation_elements_present"}

        # 2. Instagram Navigator profile state detection
        if "hasCanonicalProfile" in expression or "profile_elements_fallback" in expression or "unrecognized_dom_structure" in expression:
            if self._page_type == "CHALLENGE":
                return {"status": "ACCESS_BLOCKED", "reason": "access_blocked_or_challenge"}
            if self._page_type == "LOGIN":
                return {"status": "LOGIN_REQUIRED", "reason": "login_redirect_or_form"}
            return {"status": "AVAILABLE", "reason": "instagram_profile_detected"}

        # 3. Profile Reader DOM extraction
        if "follower_count_text" in expression and "post_count_text" in expression:
            return {
                "url": self._current_url,
                "username": self._current_username,
                "display_name": self._current_username.capitalize(),
                "follower_count_text": "1,000",
                "following_count_text": "500",
                "post_count_text": "100",
                "bio": "Realistic test profile",
                "is_verified": False,
                "is_private": False,
                "can_message": True,
                "page_missing": False,
            }

        # 4. Message Composer - open message dialog
        if "messageBtn.dispatchEvent" in expression or "messageBtn" in expression:
            self._dialog_open = True
            return {"success": True}

        # 5. Message Composer - check if input field is ready
        if 'div[aria-label*="Message"]' in expression and "return !!" in expression:
            return self._dialog_open

        # 6. Message Composer - compose message text into input
        if "messageText" in expression or "input.value = messageText" in expression:
            self._composed_text = str(arg) if arg is not None else ""
            return {"success": True}

        # 7. Message Sender - submit send
        if "Action Blocked" in expression or "composer_cleared" in expression:
            if self._page_type == "ACCESS_BLOCKED":
                return {"submitted": False, "reason": "action_blocked", "error_type": "ACCESS_PROHIBITED"}
            # Send message and clear composer
            if self._composed_text:
                self._sent_messages.append(self._composed_text)
            self._composed_text = ""
            return {"submitted": True, "composer_cleared": True}

        # 8. Send Verifier - verify sent message in thread container
        if "expectedText" in expression or "matchedSnippet" in expression:
            expected = str(arg).strip().lower() if arg is not None else ""
            matched = any(expected in m.strip().lower() for m in self._sent_messages)
            if matched:
                return {"found": True, "snippet": arg, "failure_indicator": False}
            return {"found": False, "snippet": None, "failure_indicator": False}

        # Default fallback
        if "return" in expression and "true" in expression.lower():
            return True
        return {}

    def screenshot(self, path: str) -> None:
        pass

    def get_content(self) -> str:
        return f"<html><body><h1>Instagram</h1><p>{self._current_url}</p></body></html>"


def create_realistic_session(session_id: str = "SESS-REALISTIC-P4") -> BrowserSessionInstance:
    """Create a BrowserSessionInstance backed by RealisticBrowserDriverHarness."""
    driver = RealisticBrowserDriverHarness()
    driver.launch()
    session = BrowserSessionInstance(session_id=session_id, driver=driver)
    session.status = SessionStatus.READY
    return session
