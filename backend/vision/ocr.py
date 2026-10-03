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


class StandardVisionService(VisionService):
    """
    Production Vision Service providing OCR with confidence estimation and deterministic fallback:
    DOM extraction -> if insufficient -> OCR/vision -> confidence -> verification gate.
    Never allows raw OCR output to directly authorize sending without passing the verification engine.
    """

    def __init__(self, confidence_threshold: float = 0.85):
        self.confidence_threshold = confidence_threshold

    def extract_text(self, image_path: str, region: Optional[tuple[int, int, int, int]] = None) -> str:
        text, confidence, reason = self.extract_with_confidence(image_path, region)
        if confidence < self.confidence_threshold:
            return ""
        return text

    def extract_with_confidence(
        self, image_path: str, region: Optional[tuple[int, int, int, int]] = None
    ) -> tuple[str, float, str]:
        """
        Extract text with bounded confidence score (0.0 to 1.0) and failure diagnosis.
        Returns: (extracted_text, confidence, reason)
        """
        import os
        if not image_path or not os.path.isfile(image_path):
            return "", 0.0, "image_unavailable"

        try:
            from PIL import Image
            img = Image.open(image_path)
            if region:
                img = img.crop(region)
        except Exception:
            return "", 0.0, "image_unreadable"

        # Check for optional tesseract OCR engine
        try:
            import pytesseract
            data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
            n_boxes = len(data.get("text", []))
            confidences = []
            extracted_words = []
            for i in range(n_boxes):
                w = data["text"][i].strip()
                c = int(data.get("conf", [0])[i])
                if w and c > 0:
                    extracted_words.append(w)
                    confidences.append(c / 100.0)
            if not extracted_words:
                return "", 0.0, "no_text_detected"
            mean_conf = sum(confidences) / len(confidences) if confidences else 0.0
            return " ".join(extracted_words), mean_conf, "ocr_success"
        except (ImportError, Exception):
            # Optional dependency not installed or tesseract binary not available
            # Deterministic non-send state with zero confidence
            return "", 0.0, "ocr_engine_not_installed"

