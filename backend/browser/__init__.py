"""Browser management subsystem for controlled web automation."""

from backend.browser.browser_types import (
    BrowserType,
    BrowserStatus,
    SessionStatus,
    PageStatus,
    BrowserLaunchConfig,
    BrowserHealthResult,
    BrowserSessionInfo,
)
from backend.browser.driver import BrowserDriver, PlaywrightBrowserDriver
from backend.browser.session import BrowserSessionInstance
from backend.browser.manager import BrowserManager
from backend.browser.profiles import BrowserProfile, BrowserProfileManager
from backend.browser.lifecycle import BrowserLifecycleManager
from backend.browser.health import BrowserHealthChecker
from backend.browser.exceptions import (
    BrowserException,
    BrowserLaunchError,
    BrowserCrashError,
    BrowserTimeoutError,
    BrowserNavigationError,
    BrowserSessionError,
)

__all__ = [
    "BrowserType",
    "BrowserStatus",
    "SessionStatus",
    "PageStatus",
    "BrowserLaunchConfig",
    "BrowserHealthResult",
    "BrowserSessionInfo",
    "BrowserDriver",
    "PlaywrightBrowserDriver",
    "BrowserSessionInstance",
    "BrowserManager",
    "BrowserProfile",
    "BrowserProfileManager",
    "BrowserLifecycleManager",
    "BrowserHealthChecker",
    "BrowserException",
    "BrowserLaunchError",
    "BrowserCrashError",
    "BrowserTimeoutError",
    "BrowserNavigationError",
    "BrowserSessionError",
]
