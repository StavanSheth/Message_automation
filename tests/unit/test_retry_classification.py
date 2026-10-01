"""Unit tests for RetryPolicyEngine error classification and backoff (Section 8)."""

import pytest
from backend.domain.enums import RetryClass, ErrorResolution
from backend.scheduler.retry_scheduler import RetryPolicyEngine


def test_classify_all_11_error_categories():
    engine = RetryPolicyEngine(base_delay=1.0, max_delay=60.0)

    # 1. NETWORK
    assert engine.classify_category("ECONNRESET") == RetryClass.NETWORK
    assert engine.classify_category("net_timeout") == RetryClass.NETWORK

    # 2. BROWSER
    assert engine.classify_category("BROWSER_CRASH") == RetryClass.BROWSER
    assert engine.classify_category("SESSION_DISCONNECTED") == RetryClass.BROWSER

    # 3. NAVIGATION
    assert engine.classify_category("NAVIGATION_TIMEOUT") == RetryClass.NAVIGATION

    # 4. RATE_LIMIT
    assert engine.classify_category("RATE_LIMIT") == RetryClass.RATE_LIMIT
    assert engine.classify_category("429_TOO_MANY_REQUESTS") == RetryClass.RATE_LIMIT

    # 5. TEMPORARY_INSTAGRAM
    assert engine.classify_category("INSTAGRAM_500") == RetryClass.TEMPORARY_INSTAGRAM
    assert engine.classify_category("SERVER_BUSY") == RetryClass.TEMPORARY_INSTAGRAM

    # 6. MESSAGE_SEND
    assert engine.classify_category("UNKNOWN_SEND_RESULT") == RetryClass.MESSAGE_SEND
    assert engine.classify_category("SEND_TIMEOUT") == RetryClass.MESSAGE_SEND

    # 7. VERIFICATION
    assert engine.classify_category("VERIFICATION_UNKNOWN") == RetryClass.VERIFICATION

    # 8. AUTHENTICATION
    assert engine.classify_category("LOGIN_REQUIRED") == RetryClass.AUTHENTICATION
    assert engine.classify_category("SESSION_EXPIRED") == RetryClass.AUTHENTICATION

    # 9. ACCESS_BLOCKED
    assert engine.classify_category("ACTION_BLOCKED") == RetryClass.ACCESS_BLOCKED
    assert engine.classify_category("CHALLENGE_REQUIRED") == RetryClass.ACCESS_BLOCKED
    assert engine.classify_category("CHECKPOINT") == RetryClass.ACCESS_BLOCKED

    # 10. PROFILE_MISMATCH
    assert engine.classify_category("PROFILE_MISMATCH") == RetryClass.PROFILE_MISMATCH
    assert engine.classify_category("WRONG_ACCOUNT") == RetryClass.PROFILE_MISMATCH

    # 11. UNKNOWN
    assert engine.classify_category("SOMETHING_TOTALLY_RANDOM") == RetryClass.UNKNOWN


def test_get_resolution_mappings():
    engine = RetryPolicyEngine()

    # Retryable categories
    assert engine.get_resolution(RetryClass.NETWORK) == ErrorResolution.RETRYABLE
    assert engine.get_resolution(RetryClass.BROWSER) == ErrorResolution.RETRYABLE
    assert engine.get_resolution(RetryClass.NAVIGATION) == ErrorResolution.RETRYABLE
    assert engine.get_resolution(RetryClass.RATE_LIMIT) == ErrorResolution.RETRYABLE
    assert engine.get_resolution(RetryClass.TEMPORARY_INSTAGRAM) == ErrorResolution.RETRYABLE

    # Reconciliation required (ambiguous send / verification failure)
    assert engine.get_resolution(RetryClass.MESSAGE_SEND) == ErrorResolution.RECONCILIATION_REQUIRED
    assert engine.get_resolution(RetryClass.VERIFICATION) == ErrorResolution.RECONCILIATION_REQUIRED

    # Manual review required (safety terminal states)
    assert engine.get_resolution(RetryClass.AUTHENTICATION) == ErrorResolution.MANUAL_REVIEW_REQUIRED
    assert engine.get_resolution(RetryClass.ACCESS_BLOCKED) == ErrorResolution.MANUAL_REVIEW_REQUIRED
    assert engine.get_resolution(RetryClass.PROFILE_MISMATCH) == ErrorResolution.MANUAL_REVIEW_REQUIRED
    assert engine.get_resolution(RetryClass.UNKNOWN) == ErrorResolution.MANUAL_REVIEW_REQUIRED


def test_compute_backoff_delay_capped_with_jitter():
    engine = RetryPolicyEngine(base_delay=10.0, max_delay=100.0, jitter_factor=0.2)

    # Attempt 1: base_delay * 2^0 = 10.0 + jitter in [0, 2.0]
    delay1 = engine.compute_backoff_delay(attempt=1)
    assert 10.0 <= delay1 <= 12.0

    # Attempt 2: base_delay * 2^1 = 20.0 + jitter in [0, 4.0]
    delay2 = engine.compute_backoff_delay(attempt=2)
    assert 20.0 <= delay2 <= 24.0

    # Attempt 10: would be 10 * 2^9 = 5120.0, capped to max_delay 100.0 + jitter
    delay10 = engine.compute_backoff_delay(attempt=10)
    assert 100.0 <= delay10 <= 120.0


def test_should_retry_respects_max_attempts_and_non_retryable():
    engine = RetryPolicyEngine(max_attempts=3)

    # Retryable error within limit
    assert engine.should_retry("NET_TIMEOUT", attempt=1) is True
    assert engine.should_retry("NET_TIMEOUT", attempt=2) is True
    assert engine.should_retry("NET_TIMEOUT", attempt=3) is False  # Reached max_attempts

    # Non-retryable errors must NEVER retry even at attempt 1
    assert engine.should_retry("UNKNOWN_SEND_RESULT", attempt=1) is False
    assert engine.should_retry("PROFILE_MISMATCH", attempt=1) is False
    assert engine.should_retry("LOGIN_REQUIRED", attempt=1) is False
    assert engine.should_retry("CHALLENGE", attempt=1) is False
