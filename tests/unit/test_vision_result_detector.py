"""Unit tests for the Vision / OCR pipeline and VisionResultDetector."""

import os
import tempfile
import pytest

from backend.vision.ocr import VisionPipeline, StandardVisionService, OCRResult
from backend.result_detection.detector import VisionResultDetector, ResultCode


# Helper to generate a minimal valid PNG file
VALID_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\n\x00\x00\x00\n\x08\x02"
    b"\x00\x00\x00\x02PX\xea\x00\x00\x00\x16IDATx\x9cc\xfc\xff\xff?\x03n\xc0"
    b"\x84G\x8ea\xe4J\x03\x00\xa5\xe3\x03\x11\xc7z\x1cU\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture
def valid_screenshot():
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(VALID_PNG_BYTES)
        f.flush()
        path = f.name
    yield path
    if os.path.exists(path):
        os.remove(path)


def test_successful_ocr(valid_screenshot):
    """Vision pipeline successfully extracts text with high confidence and classifies outcome."""
    pipeline = VisionPipeline(confidence_threshold=0.85)

    def mock_engine(img):
        return ("Message Sent Successfully", 0.95)

    res = pipeline.process(valid_screenshot, ocr_engine=mock_engine)
    assert res.status == "PASS"
    assert res.classification == "SUCCESS"
    assert res.confidence == 0.95
    assert "message sent" in res.normalized_text

    detector = VisionResultDetector(vision_pipeline=pipeline)
    code, conf, reason = detector.detect_with_confidence({
        "screenshot_path": valid_screenshot,
        "ocr_engine": mock_engine,
    })
    assert code == ResultCode.SUCCESS
    assert conf == 0.95


def test_bad_screenshot_corrupted():
    """Vision pipeline rejects corrupted or invalid image format safely."""
    pipeline = VisionPipeline(confidence_threshold=0.85)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"NOT_A_VALID_IMAGE_CONTENT")
        f.flush()
        bad_path = f.name

    try:
        res = pipeline.process(bad_path)
        assert res.status == "UNKNOWN"
        assert res.classification == "UNKNOWN"
        assert res.confidence == 0.0
        assert res.reason == "corrupted_or_invalid_image_format"

        detector = VisionResultDetector(vision_pipeline=pipeline)
        code = detector.detect_result({"screenshot_path": bad_path})
        assert code == ResultCode.UNKNOWN
    finally:
        if os.path.exists(bad_path):
            os.remove(bad_path)


def test_bad_screenshot_missing():
    """Vision pipeline fails safely when screenshot does not exist."""
    pipeline = VisionPipeline()
    res = pipeline.process("/path/to/nonexistent/image.png")
    assert res.status == "UNKNOWN"
    assert res.confidence == 0.0
    assert res.reason == "image_unavailable"


def test_blank_screenshot():
    """Vision pipeline correctly detects 0-byte or blank OCR output."""
    pipeline = VisionPipeline()
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        # Zero bytes file
        f.flush()
        empty_path = f.name

    try:
        res = pipeline.process(empty_path)
        assert res.status == "UNKNOWN"
        assert res.confidence == 0.0
        assert res.is_blank is True
        assert res.reason == "empty_image_file"
    finally:
        if os.path.exists(empty_path):
            os.remove(empty_path)


def test_blank_ocr_output(valid_screenshot):
    """When OCR returns no words, pipeline flags blank result with UNKNOWN."""
    pipeline = VisionPipeline()

    def mock_empty_engine(img):
        return ("", 0.0)

    res = pipeline.process(valid_screenshot, ocr_engine=mock_empty_engine)
    assert res.status == "UNKNOWN"
    assert res.is_blank is True
    assert res.confidence == 0.0


def test_low_confidence(valid_screenshot):
    """Confidence below threshold must fail closed to UNKNOWN, never guessing."""
    pipeline = VisionPipeline(confidence_threshold=0.85)

    def mock_low_conf_engine(img):
        return ("Message Sent", 0.60)  # 60% < 85% threshold

    res = pipeline.process(valid_screenshot, ocr_engine=mock_low_conf_engine)
    assert res.status == "UNKNOWN"
    assert res.classification == "UNKNOWN"
    assert res.confidence == 0.60
    assert "low_confidence" in res.reason

    detector = VisionResultDetector(vision_pipeline=pipeline)
    code = detector.detect_result({
        "screenshot_path": valid_screenshot,
        "ocr_engine": mock_low_conf_engine,
    })
    assert code == ResultCode.UNKNOWN


def test_unexpected_ui(valid_screenshot):
    """When high-confidence OCR returns unrecognized UI text, returns UNKNOWN."""
    pipeline = VisionPipeline(confidence_threshold=0.85)

    def mock_unexpected_engine(img):
        return ("Random advertisement banner and unrelated text", 0.92)

    res = pipeline.process(valid_screenshot, ocr_engine=mock_unexpected_engine)
    assert res.status == "UNKNOWN"
    assert res.classification == "UNKNOWN"
    assert res.reason == "unrecognized_ui"
    assert res.confidence == 0.92


def test_security_challenge_detected(valid_screenshot):
    """When OCR detects Instagram checkpoint / challenge, returns CHALLENGE_REQUIRED."""
    pipeline = VisionPipeline(confidence_threshold=0.85)

    def mock_challenge_engine(img):
        return ("Suspicious Activity Detected. Please confirm your info.", 0.95)

    detector = VisionResultDetector(vision_pipeline=pipeline)
    code, conf, reason = detector.detect_with_confidence({
        "screenshot_path": valid_screenshot,
        "ocr_engine": mock_challenge_engine,
    })
    assert code == ResultCode.CHALLENGE_REQUIRED
    assert conf == 0.95
    assert reason == "security_checkpoint_detected"


def test_profile_not_found_detected(valid_screenshot):
    """When OCR detects page not found, returns PROFILE_NOT_FOUND."""
    pipeline = VisionPipeline(confidence_threshold=0.85)

    def mock_not_found_engine(img):
        return ("Sorry, this page isn't available. The link may be broken.", 0.90)

    detector = VisionResultDetector(vision_pipeline=pipeline)
    code = detector.detect_result({
        "screenshot_path": valid_screenshot,
        "ocr_engine": mock_not_found_engine,
    })
    assert code == ResultCode.PROFILE_NOT_FOUND


def test_missing_or_invalid_region(valid_screenshot):
    """Invalid region bounds return clean error without crashing."""
    pipeline = VisionPipeline()
    # left >= right or top >= bottom
    res = pipeline.process(valid_screenshot, region=(100, 100, 50, 50))
    assert res.status == "UNKNOWN"
    assert res.reason == "invalid_region_coordinates"


def test_ocr_engine_unavailable(valid_screenshot):
    """When no engine is supplied and tesseract is unavailable, fails gracefully."""
    pipeline = VisionPipeline()
    res = pipeline.process(valid_screenshot)
    assert res.status == "UNKNOWN"
    assert res.confidence == 0.0
    assert res.reason in ("ocr_engine_unavailable", "no_text_detected")


def test_valid_jpeg_format(tmp_path):
    """Vision pipeline processes valid JPEG images correctly."""
    import io
    from PIL import Image

    jpeg_path = str(tmp_path / "test_image.jpg")
    img = Image.new("RGB", (20, 20), color="white")
    img.save(jpeg_path, format="JPEG")

    pipeline = VisionPipeline(confidence_threshold=0.85)

    def mock_engine(i):
        return ("Message Sent", 0.95)

    res = pipeline.process(jpeg_path, ocr_engine=mock_engine)
    assert res.status == "PASS"
    assert res.classification == "SUCCESS"
    assert res.confidence == 0.95


def test_login_detected(valid_screenshot):
    """When OCR detects login screen, returns LOGIN_REQUIRED."""
    pipeline = VisionPipeline(confidence_threshold=0.85)

    def mock_login_engine(img):
        return ("Log In to Instagram. Switch Accounts.", 0.95)

    detector = VisionResultDetector(vision_pipeline=pipeline)
    code, conf, reason = detector.detect_with_confidence({
        "screenshot_path": valid_screenshot,
        "ocr_engine": mock_login_engine,
    })
    assert code == ResultCode.LOGIN_REQUIRED
    assert conf == 0.95


def test_dm_restriction_detected(valid_screenshot):
    """When OCR detects DM restriction, returns DM_NOT_AVAILABLE."""
    pipeline = VisionPipeline(confidence_threshold=0.85)

    def mock_dm_engine(img):
        return ("You can't message this account. Restricted profile.", 0.92)

    detector = VisionResultDetector(vision_pipeline=pipeline)
    code, conf, reason = detector.detect_with_confidence({
        "screenshot_path": valid_screenshot,
        "ocr_engine": mock_dm_engine,
    })
    assert code == ResultCode.DM_NOT_AVAILABLE
    assert conf == 0.92
