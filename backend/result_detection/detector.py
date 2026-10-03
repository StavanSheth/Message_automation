"""Result detector contract and safe outcome classification."""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, Tuple
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


class VisionResultDetector(ResultDetector):
    """
    Combines direct DOM flags with the vision/OCR pipeline to classify outcomes.
    Fails closed to ResultCode.UNKNOWN if vision confidence is below threshold.
    """

    def __init__(self, vision_pipeline: Optional[Any] = None, confidence_threshold: float = 0.85):
        from backend.vision.ocr import VisionPipeline
        self.pipeline = vision_pipeline or VisionPipeline(confidence_threshold=confidence_threshold)
        self.default_detector = DefaultResultDetector()

    def detect_result(self, browser_state: Dict[str, Any]) -> str:
        code, _conf, _reason = self.detect_with_confidence(browser_state)
        return code

    def detect_with_confidence(self, browser_state: Dict[str, Any]) -> tuple[str, float, str]:
        if not browser_state:
            return ResultCode.UNKNOWN, 0.0, "empty_browser_state"

        # 1. First check explicit strong DOM flags
        dom_result = self.default_detector.detect_result(browser_state)
        if dom_result != ResultCode.UNKNOWN:
            return dom_result, 1.0, "dom_detection"

        # 2. If screenshot path is provided, process through vision pipeline
        screenshot_path = browser_state.get("screenshot_path") or browser_state.get("image_path")
        if screenshot_path:
            region = browser_state.get("region")
            target_phrases = browser_state.get("target_phrases")
            ocr_engine = browser_state.get("ocr_engine")
            res = self.pipeline.process(
                image_path=screenshot_path,
                region=region,
                target_phrases=target_phrases,
                ocr_engine=ocr_engine,
            )
            # Map classification string to ResultCode
            if res.status == "PASS" and res.classification == "SUCCESS":
                return ResultCode.SUCCESS, res.confidence, res.reason
            elif res.classification == "CHALLENGE_REQUIRED":
                return ResultCode.CHALLENGE_REQUIRED, res.confidence, res.reason
            elif res.classification == "PROFILE_NOT_FOUND":
                return ResultCode.PROFILE_NOT_FOUND, res.confidence, res.reason
            elif res.classification == "LOGIN_REQUIRED":
                return ResultCode.LOGIN_REQUIRED, res.confidence, res.reason
            elif res.classification == "DM_NOT_AVAILABLE":
                return ResultCode.DM_NOT_AVAILABLE, res.confidence, res.reason
            else:
                return ResultCode.UNKNOWN, res.confidence, res.reason

        return ResultCode.UNKNOWN, 0.0, "no_detection_signals"
