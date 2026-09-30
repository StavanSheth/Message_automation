"""Unit tests for OCR/Vision contract and placeholder behavior."""

import pytest
from backend.domain.enums import ErrorCode
from backend.domain.errors import AutomationError
from backend.vision.ocr import PlaceholderVisionService


def test_ocr_placeholder_fails_explicitly():
    service = PlaceholderVisionService()
    with pytest.raises(AutomationError) as exc_info:
        service.extract_text("fake_path.png")

    assert exc_info.value.code == ErrorCode.OCR_LOW_CONFIDENCE
    assert "not implemented in Phase 1" in exc_info.value.message
