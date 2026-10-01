"""Verification Engine interface contract and service."""

from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
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
    def decide(
        self, confidence: float, threshold: float = 0.85, signals: Optional[Dict[str, Any]] = None
    ) -> VerificationDecision:
        """Map confidence score and signals to a VerificationDecision."""
        pass


class SimpleVerificationEngine(VerificationEngine):
    """Deterministic verification engine implementing signal weighting and explicit decisions."""

    def extract_signals(self, expected: Contact, observed: Dict[str, Any]) -> Dict[str, Any]:
        signals: Dict[str, Any] = {}

        if observed.get("not_found") or observed.get("page_missing"):
            signals["not_found"] = True
            return signals

        observed_url = observed.get("url", "").rstrip("/").lower()
        expected_url = expected.instagram_url.rstrip("/").lower()
        signals["url_match"] = bool(observed_url and observed_url == expected_url)

        exp_username = expected.username
        if not exp_username and expected.instagram_url:
            parts = [p for p in expected.instagram_url.rstrip("/").split("/") if p and "instagram.com" not in p]
            if parts:
                exp_username = parts[-1]

        if exp_username and observed.get("username"):
            signals["username_match"] = (
                exp_username.strip().lower() == str(observed["username"]).strip().lower()
            )
        else:
            signals["username_match"] = False

        if expected.name and observed.get("display_name"):
            signals["name_match"] = (
                expected.name.strip().lower() in str(observed["display_name"]).strip().lower()
            )
        else:
            signals["name_match"] = False

        if expected.expected_followers is not None and observed.get("follower_count") is not None:
            diff = abs(expected.expected_followers - int(observed["follower_count"]))
            signals["follower_diff"] = diff
            signals["follower_close"] = diff <= max(10, int(expected.expected_followers * 0.1))
        else:
            signals["follower_close"] = False

        # Detect total profile mismatch when an alternate profile was loaded
        if observed.get("username") and exp_username:
            if not signals["username_match"] and not signals["url_match"]:
                signals["mismatch"] = True

        return signals

    def calculate_confidence(self, signals: Dict[str, Any]) -> float:
        if signals.get("not_found"):
            return 0.0

        score = 0.0
        # Weighted scoring according to Document 3
        if signals.get("url_match"):
            score += 0.50
        if signals.get("username_match"):
            score += 0.30
        if signals.get("name_match"):
            score += 0.15
        if signals.get("follower_close"):
            score += 0.05

        return min(1.0, round(score, 4))

    def decide(
        self, confidence: float, threshold: float = 0.85, signals: Optional[Dict[str, Any]] = None
    ) -> VerificationDecision:
        if signals and signals.get("not_found"):
            return VerificationDecision.NOT_FOUND

        if signals and signals.get("mismatch"):
            return VerificationDecision.MISMATCH

        if confidence >= threshold:
            return VerificationDecision.HIGH_CONFIDENCE
        elif confidence >= 0.50:
            return VerificationDecision.MEDIUM_CONFIDENCE
        return VerificationDecision.LOW_CONFIDENCE
