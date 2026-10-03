"""Unit test suite for StandardVisionService."""

import pytest
from backend.vision.ocr import StandardVisionService, PlaceholderVisionService
from backend.domain.errors import AutomationError


class TestVisionService:
    def test_placeholder_raises_explicit_domain_error(self):
        service = PlaceholderVisionService()
        with pytest.raises(AutomationError):
            service.extract_text("non_existent.png")

    def test_standard_vision_service_missing_image(self):
        service = StandardVisionService()
        text, conf, reason = service.extract_with_confidence("non_existent_file.png")
        assert text == ""
        assert conf == 0.0
        assert reason == "image_unavailable"
        assert service.extract_text("non_existent_file.png") == ""
