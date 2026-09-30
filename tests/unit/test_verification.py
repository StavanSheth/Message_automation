"""Unit tests for Verification Engine signals, scoring, thresholds, and decisions."""

import pytest
from backend.domain.models import Contact
from backend.domain.enums import VerificationDecision
from backend.verification.service import SimpleVerificationEngine


@pytest.fixture
def engine():
    return SimpleVerificationEngine()


@pytest.fixture
def contact():
    return Contact(
        id="C-VERIF",
        name="Jane Doe",
        instagram_url="https://instagram.com/janedoe",
        username="janedoe",
        expected_followers=5000,
    )


def test_verification_exact_all_matches(engine, contact):
    observed = {
        "url": "https://instagram.com/janedoe",
        "username": "janedoe",
        "display_name": "Jane Doe | Studio",
        "follower_count": 5050,
    }
    signals = engine.extract_signals(contact, observed)
    assert signals["url_match"] is True
    assert signals["username_match"] is True
    assert signals["name_match"] is True
    assert signals["follower_close"] is True

    confidence = engine.calculate_confidence(signals)
    assert confidence == 1.0  # 0.50 + 0.30 + 0.15 + 0.05
    decision = engine.decide(confidence, threshold=0.85, signals=signals)
    assert decision == VerificationDecision.HIGH_CONFIDENCE


def test_verification_threshold_boundaries(engine, contact):
    # Confidence exactly at 0.85 (URL + username + follower = 0.85, name missing)
    observed = {
        "url": "https://instagram.com/janedoe",
        "username": "janedoe",
        "display_name": "JD",  # doesn't match
        "follower_count": 5010,
    }
    signals = engine.extract_signals(contact, observed)
    confidence = engine.calculate_confidence(signals)
    assert confidence == 0.85
    assert engine.decide(confidence, threshold=0.85, signals=signals) == VerificationDecision.HIGH_CONFIDENCE
    assert engine.decide(confidence, threshold=0.90, signals=signals) == VerificationDecision.MEDIUM_CONFIDENCE


def test_verification_partial_match(engine, contact):
    # Only URL matches (0.50)
    observed = {
        "url": "https://instagram.com/janedoe",
        "username": "different_handle",
        "display_name": "Different Name",
        "follower_count": 100,
    }
    signals = engine.extract_signals(contact, observed)
    confidence = engine.calculate_confidence(signals)
    assert confidence == 0.50
    assert engine.decide(confidence, threshold=0.85, signals=signals) == VerificationDecision.MEDIUM_CONFIDENCE


def test_verification_no_match(engine, contact):
    observed = {
        "url": "https://instagram.com/unknown_person",
        "username": "unknown_person",
        "display_name": "Unknown",
        "follower_count": 10,
    }
    signals = engine.extract_signals(contact, observed)
    confidence = engine.calculate_confidence(signals)
    assert confidence == 0.0
    assert engine.decide(confidence, threshold=0.85, signals=signals) == VerificationDecision.MISMATCH


def test_verification_profile_not_found(engine, contact):
    observed = {"not_found": True}
    signals = engine.extract_signals(contact, observed)
    assert signals["not_found"] is True
    confidence = engine.calculate_confidence(signals)
    assert confidence == 0.0
    assert engine.decide(confidence, threshold=0.85, signals=signals) == VerificationDecision.NOT_FOUND
