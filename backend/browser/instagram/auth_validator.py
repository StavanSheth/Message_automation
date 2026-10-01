"""Instagram session authentication validator detecting login, challenge, and session state."""

from typing import Dict, Any, Tuple
from backend.browser.session import BrowserSessionInstance
from backend.domain.enums import SessionAuthState
from backend.events.logger import get_logger

logger = get_logger("instagram_auth_validator")


class InstagramAuthValidator:
    """Validates Instagram authentication state on an active browser session."""

    def check_auth_state(self, session: BrowserSessionInstance) -> Tuple[SessionAuthState, str]:
        """
        Evaluate active session to determine Instagram authentication state:
        Returns (SessionAuthState, reason).
        """
        if not session or not session.is_alive():
            return SessionAuthState.UNKNOWN, "session_not_alive"

        result = session.evaluate(
            """() => {
                const url = window.location.href || '';
                const body = document.body ? (document.body.innerText || '').substring(0, 3000) : '';

                // 1. Checkpoint or Challenge
                if (url.includes('/challenge/') || url.includes('/checkpoint/') || body.includes('Help us confirm')) {
                    return { state: 'CHALLENGE', reason: 'security_challenge_or_checkpoint' };
                }

                // 2. Session expired
                if (body.includes('Session expired') || body.includes('Please log back in') || body.includes('Logged out')) {
                    return { state: 'SESSION_EXPIRED', reason: 'session_expired_detected' };
                }

                // 3. Login required / form detected
                if (
                    url.includes('/accounts/login') ||
                    url.includes('/accounts/emailsignup') ||
                    document.querySelector('form#loginForm') ||
                    document.querySelector('input[name="username"]') && document.querySelector('input[name="password"]') ||
                    body.includes('Log In to Instagram')
                ) {
                    return { state: 'LOGIN_REQUIRED', reason: 'login_form_present' };
                }

                // 3. Authenticated indicators
                const hasInbox = !!document.querySelector('a[href*="/direct/inbox"], svg[aria-label*="Direct" i], svg[aria-label*="Messenger" i]');
                const hasNav = !!document.querySelector('nav, div[role="navigation"]');
                const hasProfileNav = !!document.querySelector('a[href*="/reels/"], a[href*="/explore/"]');

                if (hasInbox || (hasNav && hasProfileNav)) {
                    return { state: 'AUTHENTICATED', reason: 'navigation_elements_present' };
                }

                return { state: 'UNKNOWN', reason: 'unrecognized_auth_state' };
            }"""
        )

        if not isinstance(result, dict):
            return SessionAuthState.UNKNOWN, "evaluation_failed"

        state_str = result.get("state", "UNKNOWN")
        reason = result.get("reason", "")
        auth_state = SessionAuthState(state_str) if state_str in SessionAuthState.__members__ else SessionAuthState.UNKNOWN

        return auth_state, reason
