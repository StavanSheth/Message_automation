"""Instagram Navigator abstraction for profile navigation and session detection."""

import re
from typing import Dict, Any, Optional
from urllib.parse import urlparse, urlunparse

from backend.browser.session import BrowserSessionInstance
from backend.browser.exceptions import (
    BrowserException,
    BrowserTimeoutError,
    BrowserCrashError,
    BrowserNavigationError,
)
from backend.domain.enums import ErrorCode
from backend.domain.errors import AutomationError
from backend.events.logger import get_logger

logger = get_logger("instagram_navigator")


class InstagramPageStatus:
    AVAILABLE = "AVAILABLE"
    NOT_FOUND = "NOT_FOUND"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    RESTRICTED = "RESTRICTED"
    UNAVAILABLE = "UNAVAILABLE"


class InstagramNavigator:
    """
    Handles Instagram URL normalization, navigation, and page state classification.
    All operations execute through the existing BrowserSessionInstance abstraction.
    """

    @staticmethod
    def normalize_url(raw_input: str) -> str:
        """
        Normalize arbitrary URL or username string to canonical Instagram profile URL:
        - '@user' -> 'https://www.instagram.com/user/'
        - 'instagram.com/user' -> 'https://www.instagram.com/user/'
        - 'http://...' -> 'https://...'
        - Strips query params and hashes.
        """
        if not raw_input or not isinstance(raw_input, str):
            raise AutomationError(code=ErrorCode.INVALID_DATA, message="Profile URL/username cannot be empty")

        cleaned = raw_input.strip()
        if cleaned.startswith("@"):
            cleaned = cleaned[1:].strip()

        if not cleaned:
            raise AutomationError(code=ErrorCode.INVALID_DATA, message="Profile username cannot be empty")

        # If it doesn't look like a URL, assume it's a bare username
        if "/" not in cleaned and "." not in cleaned:
            return f"https://www.instagram.com/{cleaned}/"

        # Add scheme if missing
        if not cleaned.startswith("http://") and not cleaned.startswith("https://"):
            cleaned = "https://" + cleaned

        parsed = urlparse(cleaned)
        scheme = "https"
        netloc = "www.instagram.com"

        # Clean path: extract the username path segment
        path_parts = [p for p in parsed.path.strip("/").split("/") if p]
        if not path_parts:
            raise AutomationError(code=ErrorCode.INVALID_DATA, message=f"No profile path found in URL: '{raw_input}'")

        username = path_parts[0]
        canonical_path = f"/{username}/"

        return urlunparse((scheme, netloc, canonical_path, "", "", ""))

    @classmethod
    def extract_username_from_url(cls, url: str) -> Optional[str]:
        """Extract username from a normalized or raw Instagram profile URL."""
        try:
            norm = cls.normalize_url(url)
            parsed = urlparse(norm)
            parts = [p for p in parsed.path.strip("/").split("/") if p]
            return parts[0] if parts else None
        except Exception:
            return None

    def navigate_to_profile(
        self,
        session: BrowserSessionInstance,
        profile_url: str,
        timeout_ms: int = 30000,
    ) -> Dict[str, Any]:
        """
        Navigate to target Instagram profile and detect page state.
        Returns diagnostic dictionary:
        {
            "status": InstagramPageStatus,
            "url": str,
            "username": Optional[str],
            "error": Optional[str],
        }
        """
        if not session or not session.is_alive():
            return {
                "status": InstagramPageStatus.UNAVAILABLE,
                "url": "",
                "username": None,
                "error": "Browser session is not alive",
            }

        try:
            target_url = self.normalize_url(profile_url)
        except AutomationError as e:
            return {
                "status": InstagramPageStatus.NOT_FOUND,
                "url": profile_url,
                "username": None,
                "error": str(e),
            }

        expected_username = self.extract_username_from_url(target_url)

        try:
            loaded_url = session.navigate(target_url, timeout_ms=timeout_ms)
        except BrowserTimeoutError as e:
            logger.warning(f"Timeout navigating to profile '{target_url}': {e}")
            return {
                "status": InstagramPageStatus.UNAVAILABLE,
                "url": target_url,
                "username": expected_username,
                "error": f"Page load timeout: {e}",
            }
        except BrowserCrashError as e:
            logger.error(f"Browser crash navigating to profile '{target_url}': {e}")
            raise
        except BrowserException as e:
            logger.error(f"Navigation error for profile '{target_url}': {e}")
            return {
                "status": InstagramPageStatus.UNAVAILABLE,
                "url": target_url,
                "username": expected_username,
                "error": str(e),
            }

        # Inspect page content via DOM evaluation
        page_state = session.evaluate(
            """() => {
                const currentUrl = window.location.href || '';
                const title = document.title || '';
                const bodyText = document.body ? (document.body.innerText || '').substring(0, 4000) : '';

                // 1. Check for login wall
                if (
                    currentUrl.includes('/accounts/login') ||
                    currentUrl.includes('/accounts/emailsignup') ||
                    bodyText.includes('Log In to Instagram') ||
                    document.querySelector('form#loginForm') ||
                    document.querySelector('input[name="username"]') && document.querySelector('input[name="password"]')
                ) {
                    return { status: 'LOGIN_REQUIRED', reason: 'login_redirect_or_form' };
                }

                // 2. Check for Page Not Found (404 / broken link)
                if (
                    bodyText.includes("Sorry, this page isn't available") ||
                    bodyText.includes("The link you followed may be broken") ||
                    title.includes('Page Not Found')
                ) {
                    return { status: 'NOT_FOUND', reason: 'page_not_found_message' };
                }

                // 3. Check for restricted / age-gated profile
                if (
                    bodyText.includes('Restricted profile') ||
                    bodyText.includes('Must be 18 or older') ||
                    bodyText.includes('You must be of legal age')
                ) {
                    return { status: 'RESTRICTED', reason: 'profile_restricted' };
                }

                // 4. Specific Instagram profile signals (strong identity verification)
                const hasCanonicalProfile = !!document.querySelector('link[rel="canonical"][href*="instagram.com/"]');
                const hasOgProfile = !!document.querySelector('meta[property="og:type"][content="profile"]') ||
                                     !!document.querySelector('meta[property="og:url"][content*="instagram.com/"]');
                const hasProfileHeader = !!document.querySelector('header section') || !!document.querySelector('header [role="img"]');
                const hasProfileButtons = !!document.querySelector('header button') || !!document.querySelector('header [role="button"]');
                const hasUsernameHeader = !!document.querySelector('header h2, section h2');
                const hasMetricList = !!document.querySelector('header ul li');

                if (hasCanonicalProfile || hasOgProfile || (hasProfileHeader && (hasProfileButtons || hasUsernameHeader || hasMetricList))) {
                    return { status: 'AVAILABLE', reason: 'instagram_profile_detected' };
                }

                // 5. Fallback for lightweight / mobile web structures
                const hasMain = !!document.querySelector('section main, main[role="main"]');
                if (hasMain && (hasUsernameHeader || document.querySelector('header'))) {
                    return { status: 'AVAILABLE', reason: 'profile_elements_fallback' };
                }

                return { status: 'AVAILABLE', reason: 'default_available' };
            }"""
        )

        detected_status = page_state.get("status", InstagramPageStatus.AVAILABLE) if isinstance(page_state, dict) else InstagramPageStatus.AVAILABLE
        reason = page_state.get("reason", "") if isinstance(page_state, dict) else ""

        return {
            "status": detected_status,
            "url": loaded_url,
            "username": expected_username,
            "error": reason if detected_status != InstagramPageStatus.AVAILABLE else None,
        }
