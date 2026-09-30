"""Unit tests for Result Detector outcome classification."""

import pytest
from backend.result_detection.detector import DefaultResultDetector, ResultCode


def test_result_detector_classifications():
    detector = DefaultResultDetector()

    # Success
    assert detector.detect_result({"is_success": True}) == ResultCode.SUCCESS

    # DM unavailable
    assert detector.detect_result({"dm_unavailable": True}) == ResultCode.DM_NOT_AVAILABLE
    assert detector.detect_result({"cannot_message": True}) == ResultCode.DM_NOT_AVAILABLE

    # Login required
    assert detector.detect_result({"login_required": True}) == ResultCode.LOGIN_REQUIRED

    # Challenge / CAPTCHA
    assert detector.detect_result({"captcha_present": True}) == ResultCode.CHALLENGE_REQUIRED

    # Profile not found & mismatch
    assert detector.detect_result({"profile_not_found": True}) == ResultCode.PROFILE_NOT_FOUND
    assert detector.detect_result({"profile_mismatch": True}) == ResultCode.PROFILE_MISMATCH

    # Network error / timeout
    assert detector.detect_result({"network_offline": True}) == ResultCode.NETWORK_ERROR
    assert detector.detect_result({"timed_out": True}) == ResultCode.TIMEOUT

    # Safe default for empty or undefined state: must be UNKNOWN, NEVER SUCCESS
    assert detector.detect_result({}) == ResultCode.UNKNOWN
    assert detector.detect_result({"random_unknown_field": 123}) == ResultCode.UNKNOWN
