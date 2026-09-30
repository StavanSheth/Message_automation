"""Result detector contract for classifying browser action outcomes."""

from abc import ABC, abstractmethod
from typing import Dict, Any


class ResultDetector(ABC):
    """Classifies observed browser state into deterministic outcome codes."""

    @abstractmethod
    def detect_result(self, browser_state: Dict[str, Any]) -> str:
        """
        Maps UI signals to:
        SUCCESS, DM_NOT_AVAILABLE, LOGIN_REQUIRED, CHALLENGE_REQUIRED,
        PROFILE_NOT_FOUND, PROFILE_MISMATCH, SEND_FAILED, NETWORK_ERROR, TIMEOUT, UNKNOWN
        """
        pass
