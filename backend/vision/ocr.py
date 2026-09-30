"""Vision and OCR engine contract."""

from abc import ABC, abstractmethod
from typing import Optional
from backend.domain.enums import ErrorCode
from backend.domain.errors import AutomationError


class VisionService(ABC):
    """Contract for image processing and optical character recognition on screenshots."""

    @abstractmethod
    def extract_text(self, image_path: str, region: Optional[tuple[int, int, int, int]] = None) -> str:
        """Extract text from screenshot or specific bounding box."""
        pass


class PlaceholderVisionService(VisionService):
    """
    Phase 1 placeholder for the OCR service.
    Fails explicitly with a controlled domain error rather than pretending OCR executed.
    """

    def extract_text(self, image_path: str, region: Optional[tuple[int, int, int, int]] = None) -> str:
        raise AutomationError(
            code=ErrorCode.OCR_LOW_CONFIDENCE,
            message="OCR engine is not implemented in Phase 1. Vision/PaddleOCR is scheduled for Phase 3.",
            retryable=False,
        )
