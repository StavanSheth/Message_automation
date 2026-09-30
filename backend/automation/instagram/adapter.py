"""Instagram Browser Adapter interface contract."""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional


class InstagramAdapter(ABC):
    """Contract for browser-driven Instagram automation."""

    @abstractmethod
    def check_login(self) -> bool:
        """Check whether the active browser profile has an authenticated Instagram session."""
        pass

    @abstractmethod
    def open_profile(self, profile_url: str) -> bool:
        """Navigate to the target Instagram profile."""
        pass

    @abstractmethod
    def extract_profile(self) -> Dict[str, Any]:
        """Extract visible profile fields (username, display name, follower count, bio, etc.)."""
        pass

    @abstractmethod
    def check_message_availability(self) -> bool:
        """Determine whether direct messaging is enabled and accessible for the target."""
        pass

    @abstractmethod
    def prepare_message(self, body: str) -> bool:
        """Input message content into the message dialog."""
        pass

    @abstractmethod
    def send_message(self) -> bool:
        """Perform send action."""
        pass

    @abstractmethod
    def detect_result(self) -> str:
        """Determine send outcome (SUCCESS, DM_NOT_AVAILABLE, UNKNOWN, etc.)."""
        pass

    @abstractmethod
    def inspect_conversation(self) -> Dict[str, Any]:
        """Inspect conversation history for unknown-send reconciliation."""
        pass


class PlaceholderInstagramAdapter(InstagramAdapter):
    """Phase 1 placeholder adapter."""

    def check_login(self) -> bool:
        return False

    def open_profile(self, profile_url: str) -> bool:
        return False

    def extract_profile(self) -> Dict[str, Any]:
        return {}

    def check_message_availability(self) -> bool:
        return False

    def prepare_message(self, body: str) -> bool:
        return False

    def send_message(self) -> bool:
        return False

    def detect_result(self) -> str:
        return "UNKNOWN"

    def inspect_conversation(self) -> Dict[str, Any]:
        return {}
