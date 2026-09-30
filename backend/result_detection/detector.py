"""Result detector contract and safe outcome classification."""

from abc import ABC, abstractmethod
from typing import Dict, Any
from backend.domain.enums import ErrorCode


# Standardized UI result codes as defined in Document 2 & 3
class ResultCode:
    SUCCESS = "SUCCESS"
    DM_NOT_AVAILABLE = "DM_NOT_AVAILABLE"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    CHALLENGE_REQUIRED = "CHALLENGE_REQUIRED"
    PROFILE_NOT_FOUND = "PROFILE_NOT_FOUND"
    PROFILE_MISMATCH = "PROFILE_MISMATCH"
    SEND_FAILED = "SEND_FAILED"
    NETWORK_ERROR = "NETWORK_ERROR"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"


class ResultDetector(ABC):
    """Classifies observed browser state into deterministic outcome codes."""

    @abstractmethod
    def detect_result(self, browser_state: Dict[str, Any]) -> str:
        """Map UI signals to a standardized ResultCode."""
        pass


class DefaultResultDetector(ResultDetector):
    """
    Standard result classifier. Safely defaults to ResultCode.UNKNOWN when UI state
    is unrecognized or undefined to prevent false successes.
    """

    def detect_result(self, browser_state: Dict[str, Any]) -> str:
        if not browser_state:
            return ResultCode.UNKNOWN

        # Check explicit flags
        if browser_state.get("is_success") is True:
            return ResultCode.SUCCESS
        if browser_state.get("dm_unavailable") is True or browser_state.get("cannot_message") is True:
            return ResultCode.DM_NOT_AVAILABLE
        if browser_state.get("login_required") is True:
            return ResultCode.LOGIN_REQUIRED
        if browser_state.get("captcha_present") is True or browser_state.get("challenge_required") is True:
            return ResultCode.CHALLENGE_REQUIRED
        if browser_state.get("profile_not_found") is True:
            return ResultCode.PROFILE_NOT_FOUND
        if browser_state.get("profile_mismatch") is True:
            return ResultCode.PROFILE_MISMATCH
        if browser_state.get("network_offline") is True:
            return ResultCode.NETWORK_ERROR
        if browser_state.get("timed_out") is True:
            return ResultCode.TIMEOUT
        if browser_state.get("send_failed") is True:
            return ResultCode.SEND_FAILED

        # Default unknown state must always be safe (never interpreted as success)
        return ResultCode.UNKNOWN
