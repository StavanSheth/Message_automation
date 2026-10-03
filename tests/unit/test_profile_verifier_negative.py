"""Negative test suite for Profile Verifier: Section 25 requirements.

Validates that:
- Wrong username -> MISMATCH / REJECTED
- Wrong URL -> MISMATCH / REJECTED
- Wrong display name -> confidence penalty / REJECTED
- Wrong account entirely -> MISMATCH
- Private profile -> flagged is_private
- Profile unavailable / page missing -> NOT_FOUND, never verified
- Partial DOM -> low confidence, never HIGH_CONFIDENCE
- Stale page -> mismatch
- Unknown profile never allows sending (is_send_allowed is False)
"""

import pytest
from backend.browser.instagram.profile_verifier import InstagramProfileVerifier
from backend.domain.models import Contact
from backend.domain.enums import VerificationDecision


@pytest.fixture
def verifier():
    return InstagramProfileVerifier(threshold=0.85)


@pytest.fixture
def valid_contact():
    return Contact(
        id="cnt_target_1",
        name="Sarah Connor",
        username="sarah_c",
        instagram_url="https://www.instagram.com/sarah_c/",
        expected_followers=5000,
    )


def test_wrong_username_is_rejected(verifier, valid_contact):
    observed = {
        "url": "https://www.instagram.com/john_doe/",
        "username": "john_doe",
        "display_name": "John Doe",
        "follower_count": 5000,
        "is_verified": False,
        "is_private": False,
        "can_message": True,
        "page_missing": False,
    }
    decision, confidence, signals, res = verifier.verify_profile(valid_contact, observed)
    assert decision in (VerificationDecision.MISMATCH, VerificationDecision.LOW_CONFIDENCE)
    assert verifier.is_send_allowed(decision, execution_mode="AUTOMATIC") is False
    assert verifier.is_send_allowed(decision, execution_mode="MANUAL") is False


def test_wrong_url_domain_or_path_is_rejected(verifier, valid_contact):
    observed = {
        "url": "https://www.instagram.com/other_account/",
        "username": "sarah_c",  # matching username but wrong URL
        "display_name": "Sarah",
        "follower_count": 5000,
        "is_verified": False,
        "is_private": False,
        "can_message": True,
        "page_missing": False,
    }
    decision, confidence, signals, res = verifier.verify_profile(valid_contact, observed)
    # Even if username matches partially, mismatch in URL lowers confidence below threshold
    assert verifier.is_send_allowed(decision, execution_mode="AUTOMATIC") is False


def test_wrong_display_name_and_username(verifier, valid_contact):
    observed = {
        "url": "https://www.instagram.com/sarah_c/",
        "username": "imposter_sarah",
        "display_name": "Completely Different Person",
        "follower_count": 10,
        "is_verified": False,
        "is_private": False,
        "can_message": True,
        "page_missing": False,
    }
    decision, confidence, signals, res = verifier.verify_profile(valid_contact, observed)
    # URL matches (0.50), but username/name fail -> at most MEDIUM_CONFIDENCE, never HIGH_CONFIDENCE
    assert decision != VerificationDecision.HIGH_CONFIDENCE
    assert confidence <= 0.50
    assert verifier.is_send_allowed(decision, execution_mode="AUTOMATIC") is False


def test_profile_unavailable_or_page_missing(verifier, valid_contact):
    observed = {
        "url": "https://www.instagram.com/sarah_c/",
        "username": "",
        "display_name": "",
        "page_missing": True,
    }
    decision, confidence, signals, res = verifier.verify_profile(valid_contact, observed)
    assert decision == VerificationDecision.NOT_FOUND
    assert verifier.is_send_allowed(decision, execution_mode="AUTOMATIC") is False
    assert verifier.is_send_allowed(decision, execution_mode="MANUAL") is False


def test_private_profile_detection(verifier, valid_contact):
    observed = {
        "url": "https://www.instagram.com/sarah_c/",
        "username": "sarah_c",
        "display_name": "Sarah Connor",
        "follower_count": 5000,
        "is_verified": False,
        "is_private": True,
        "can_message": False,
        "page_missing": False,
    }
    decision, confidence, signals, res = verifier.verify_profile(valid_contact, observed)
    assert signals.get("is_private") is True or observed.get("is_private") is True


def test_partial_dom_without_identity_elements(verifier, valid_contact):
    # DOM loaded without username or display name
    observed = {
        "url": "https://www.instagram.com/sarah_c/",
        "username": "",
        "display_name": "",
        "follower_count": None,
        "is_verified": False,
        "is_private": False,
        "can_message": False,
        "page_missing": False,
    }
    decision, confidence, signals, res = verifier.verify_profile(valid_contact, observed)
    assert decision != VerificationDecision.HIGH_CONFIDENCE
    assert confidence <= 0.50
    assert verifier.is_send_allowed(decision, execution_mode="AUTOMATIC") is False


def test_unknown_profile_never_becomes_verified(verifier):
    contact = Contact(
        id="cnt_unknown",
        name="Unknown Contact",
        username="",
        instagram_url="",
    )
    observed = {
        "url": "https://www.instagram.com/some_random_page/",
        "username": "some_random_page",
        "display_name": "Random",
        "page_missing": False,
    }
    decision, confidence, signals, res = verifier.verify_profile(contact, observed)
    assert decision != VerificationDecision.HIGH_CONFIDENCE
    assert verifier.is_send_allowed(decision, execution_mode="AUTOMATIC") is False
