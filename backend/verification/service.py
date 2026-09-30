"""Verification Engine interface contract and service."""

from abc import ABC, abstractmethod
from typing import Dict, Any, List
from backend.domain.models import VerificationResult, Contact
from backend.domain.enums import VerificationDecision


class VerificationEngine(ABC):
    """Contract for verifying target profile identity before messaging."""

    @abstractmethod
    def extract_signals(self, expected: Contact, observed: Dict[str, Any]) -> Dict[str, Any]:
        """Compare expected contact data with observed profile data."""
        pass

    @abstractmethod
    def calculate_confidence(self, signals: Dict[str, Any]) -> float:
        """Calculate aggregate confidence score between 0.0 and 1.0."""
        pass

    @abstractmethod
    def decide(self, confidence: float, threshold: float = 0.85) -> VerificationDecision:
        """Map confidence score to a VerificationDecision."""
        pass


class SimpleVerificationEngine(VerificationEngine):
    """Basic verification engine implementing signal weighting."""

    def extract_signals(self, expected: Contact, observed: Dict[str, Any]) -> Dict[str, Any]:
        signals: Dict[str, Any] = {}
        observed_url = observed.get("url", "").rstrip("/").lower()
        expected_url = expected.instagram_url.rstrip("/").lower()
        signals["url_match"] = bool(observed_url and observed_url == expected_url)

        if expected.username and observed.get("username"):
            signals["username_match"] = (
                expected.username.strip().lower() == str(observed["username"]).strip().lower()
            )

        if expected.name and observed.get("display_name"):
            signals["name_match"] = (
                expected.name.strip().lower() in str(observed["display_name"]).strip().lower()
            )

        if expected.expected_followers is not None and observed.get("follower_count") is not None:
            diff = abs(expected.expected_followers - int(observed["follower_count"]))
            signals["follower_diff"] = diff
            signals["follower_close"] = diff <= max(10, int(expected.expected_followers * 0.1))

        return signals

    def calculate_confidence(self, signals: Dict[str, Any]) -> float:
        score = 0.0
        # Weighted scoring as per Document 3
        if signals.get("url_match"):
            score += 0.50
        if signals.get("username_match"):
            score += 0.30
        if signals.get("name_match"):
            score += 0.15
        if signals.get("follower_close"):
            score += 0.05
        return min(1.0, score)

    def decide(self, confidence: float, threshold: float = 0.85) -> VerificationDecision:
        if confidence >= threshold:
            return VerificationDecision.HIGH_CONFIDENCE
        elif confidence >= 0.50:
            return VerificationDecision.MEDIUM_CONFIDENCE
        return VerificationDecision.LOW_CONFIDENCE
