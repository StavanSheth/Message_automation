"""Instagram Profile Verifier integrating browser-observed data with verification engine."""

import json
from typing import Dict, Any, Optional, Tuple

from backend.domain.models import Contact, VerificationResult, utc_now_iso
from backend.domain.enums import VerificationDecision
from backend.verification.service import VerificationEngine, SimpleVerificationEngine
from backend.repositories.verification_result_repo import VerificationResultRepository
from backend.events.correlation import generate_id
from backend.events.logger import get_logger

logger = get_logger("instagram_profile_verifier")


class InstagramProfileVerifier:
    """
    Compares expected Contact data with observed browser profile data,
    evaluates confidence and decision, and persists results to the audit repository.
    """

    def __init__(
        self,
        engine: Optional[VerificationEngine] = None,
        verification_repo: Optional[VerificationResultRepository] = None,
        threshold: float = 0.85,
    ):
        self.engine = engine or SimpleVerificationEngine()
        self.verification_repo = verification_repo
        self.threshold = threshold

    def verify_profile(
        self,
        contact: Contact,
        observed: Dict[str, Any],
        task_id: Optional[str] = None,
    ) -> Tuple[VerificationDecision, float, Dict[str, Any], VerificationResult]:
        """
        Execute deterministic identity verification against observed profile:
        1. Extract signals (URL, username, display name, followers, page missing).
        2. Calculate confidence score (0.0 to 1.0).
        3. Determine decision (HIGH, MEDIUM, LOW, MISMATCH, NOT_FOUND).
        4. Persist to VerificationResultRepository if repository is provided.
        Returns: (decision, confidence, signals, result_model)
        """
        signals = self.engine.extract_signals(contact, observed)
        confidence = self.engine.calculate_confidence(signals)
        decision = self.engine.decide(confidence, threshold=self.threshold, signals=signals)

        signals_payload = {
            "signals": signals,
            "observed": {
                "url": observed.get("url"),
                "username": observed.get("username"),
                "display_name": observed.get("display_name"),
                "follower_count": observed.get("follower_count"),
                "is_verified": observed.get("is_verified"),
                "is_private": observed.get("is_private"),
                "can_message": observed.get("can_message"),
            },
            "expected": {
                "contact_id": contact.id,
                "name": contact.name,
                "username": contact.username,
                "instagram_url": contact.instagram_url,
                "expected_followers": contact.expected_followers,
            },
        }

        result = VerificationResult(
            id=generate_id("VR"),
            contact_id=contact.id,
            task_id=task_id,
            confidence=confidence,
            decision=decision,
            signals_json=json.dumps(signals_payload),
            ocr_text=None,
            created_at=utc_now_iso(),
        )

        if self.verification_repo:
            try:
                self.verification_repo.create(result)
            except Exception as e:
                logger.error(f"Failed to persist verification result for contact {contact.id}: {e}")

        logger.info(
            "Profile verification completed",
            contact_id=contact.id,
            task_id=task_id,
            decision=decision.value,
            confidence=confidence,
        )

        return decision, confidence, signals, result

    def is_send_allowed(self, decision: VerificationDecision, execution_mode: str = "MANUAL") -> bool:
        """
        Determine if sending is allowed based on decision and execution mode:
        - HIGH_CONFIDENCE: Allowed in AUTOMATIC mode, or in MANUAL mode upon approval.
        - MEDIUM_CONFIDENCE: NEVER allowed automatically; requires MANUAL review.
        - LOW_CONFIDENCE: NEVER allowed automatically; requires MANUAL review.
        - MISMATCH: NEVER allowed under any circumstances.
        - NOT_FOUND: NEVER allowed under any circumstances.
        """
        if decision in (VerificationDecision.MISMATCH, VerificationDecision.NOT_FOUND):
            return False

        if execution_mode == "AUTOMATIC":
            return decision == VerificationDecision.HIGH_CONFIDENCE

        # In MANUAL mode, caller handles explicit user approval
        return decision in (
            VerificationDecision.HIGH_CONFIDENCE,
            VerificationDecision.MEDIUM_CONFIDENCE,
            VerificationDecision.LOW_CONFIDENCE,
        )
