"""Browser subsystem exceptions."""

from typing import Optional
from backend.domain.enums import ErrorCode
from backend.domain.errors import AutomationError


class BrowserException(AutomationError):
    """Base exception for all browser errors."""
    def __init__(self, message: str, code: ErrorCode = ErrorCode.INTERNAL_ERROR, retryable: bool = False):
        super().__init__(code=code, message=message, retryable=retryable)


class BrowserLaunchError(BrowserException):
    """Failed to launch browser process or driver."""
    def __init__(self, message: str, code: ErrorCode = ErrorCode.BROWSER_CRASH, retryable: bool = True):
        super().__init__(message=message, code=code, retryable=retryable)


class BrowserCrashError(BrowserException):
    """Browser process or context crashed unexpectedly."""
    def __init__(self, message: str, code: ErrorCode = ErrorCode.BROWSER_CRASH, retryable: bool = True):
        super().__init__(message=message, code=code, retryable=retryable)


class BrowserTimeoutError(BrowserException):
    """Browser operation timed out."""
    def __init__(self, message: str, code: ErrorCode = ErrorCode.TIMEOUT, retryable: bool = True):
        super().__init__(message=message, code=code, retryable=retryable)


class BrowserNavigationError(BrowserException):
    """Failed to navigate to target URL."""
    def __init__(self, message: str, code: ErrorCode = ErrorCode.NETWORK_OFFLINE, retryable: bool = True):
        super().__init__(message=message, code=code, retryable=retryable)


class BrowserSessionError(BrowserException):
    """Session management or state error."""
    def __init__(self, message: str, code: ErrorCode = ErrorCode.SESSION_EXPIRED, retryable: bool = False):
        super().__init__(message=message, code=code, retryable=retryable)
