"""Vision and OCR engine contract and deterministic processing pipeline."""

import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Tuple, Dict, Any, Callable, List

from backend.domain.enums import ErrorCode
from backend.domain.errors import AutomationError


@dataclass
class OCRResult:
    """Deterministic result of the vision/OCR pipeline."""
    text: str
    normalized_text: str
    confidence: float
    status: str  # "PASS", "FAIL", "UNKNOWN"
    classification: str
    reason: str
    region: Optional[Tuple[int, int, int, int]] = None
    is_blank: bool = False
    details: Dict[str, Any] = field(default_factory=dict)


class VisionService(ABC):
    """Contract for image processing and optical character recognition on screenshots."""

    @abstractmethod
    def extract_text(self, image_path: str, region: Optional[Tuple[int, int, int, int]] = None) -> str:
        """Extract text from screenshot or specific bounding box."""
        pass


class PlaceholderVisionService(VisionService):
    """
    Placeholder for the OCR service when OCR is explicitly disabled.
    Fails explicitly with a controlled domain error rather than pretending OCR executed.
    """

    def extract_text(self, image_path: str, region: Optional[Tuple[int, int, int, int]] = None) -> str:
        raise AutomationError(
            code=ErrorCode.OCR_LOW_CONFIDENCE,
            message="OCR engine is not implemented in Phase 1. Vision/PaddleOCR is scheduled for Phase 3.",
            retryable=False,
        )


class VisionPipeline:
    """
    Deterministic end-to-end vision pipeline:
    Screenshot -> Crop/region selection -> Image preprocessing -> OCR ->
    Text normalization -> Result classifier -> Confidence -> PASS / FAIL / UNKNOWN
    """

    def __init__(self, confidence_threshold: float = 0.85):
        self.confidence_threshold = confidence_threshold

    @staticmethod
    def validate_image_file(image_path: str) -> Tuple[bool, str]:
        """Validate image existence, size, and header integrity."""
        if not image_path:
            return False, "image_unavailable"
        if not os.path.isfile(image_path):
            return False, "image_unavailable"
        try:
            size = os.path.getsize(image_path)
            if size == 0:
                return False, "empty_image_file"
            with open(image_path, "rb") as f:
                header = f.read(16)
            # Check for PNG (\x89PNG\r\n\x1a\n) or JPEG (\xff\xd8\xff) or WebP (RIFF....WEBP)
            is_png = header.startswith(b"\x89PNG\r\n\x1a\n")
            is_jpeg = header.startswith(b"\xff\xd8\xff")
            is_webp = header.startswith(b"RIFF") and b"WEBP" in header
            if not (is_png or is_jpeg or is_webp):
                return False, "corrupted_or_invalid_image_format"
            return True, "valid"
        except Exception as e:
            return False, f"image_read_error: {e}"

    @staticmethod
    def normalize_text(raw_text: str) -> str:
        """Normalize extracted OCR text for deterministic comparison."""
        if not raw_text:
            return ""
        # Lowercase, replace punctuation with spaces, collapse whitespace
        text = raw_text.lower()
        text = re.sub(r"[^\w\s]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def process(
        self,
        image_path: str,
        region: Optional[Tuple[int, int, int, int]] = None,
        target_phrases: Optional[List[str]] = None,
        ocr_engine: Optional[Callable[[Any], Tuple[str, float]]] = None,
    ) -> OCRResult:
        """
        Execute full vision pipeline.
        Never guesses; returns UNKNOWN with 0.0 confidence on failures or low confidence.
        """
        # 1. Image validation
        valid, val_reason = self.validate_image_file(image_path)
        if not valid:
            return OCRResult(
                text="",
                normalized_text="",
                confidence=0.0,
                status="UNKNOWN",
                classification="UNKNOWN",
                reason=val_reason,
                region=region,
                is_blank=(val_reason == "empty_image_file"),
            )

        # 2. Region bounds validation
        if region:
            left, top, right, bottom = region
            if left < 0 or top < 0 or right <= left or bottom <= top:
                return OCRResult(
                    text="",
                    normalized_text="",
                    confidence=0.0,
                    status="UNKNOWN",
                    classification="UNKNOWN",
                    reason="invalid_region_coordinates",
                    region=region,
                )

        # 3. Preprocessing (Crop and contrast if PIL available)
        preprocessed_img = None
        try:
            from PIL import Image, ImageOps
            img = Image.open(image_path)
            if region:
                img = img.crop(region)
            # Convert to grayscale and equalize contrast for higher OCR readability
            img_gray = ImageOps.grayscale(img)
            preprocessed_img = ImageOps.autocontrast(img_gray)
        except ImportError:
            # Fall back to raw file if PIL is not installed
            preprocessed_img = image_path
        except Exception as e:
            return OCRResult(
                text="",
                normalized_text="",
                confidence=0.0,
                status="UNKNOWN",
                classification="UNKNOWN",
                reason=f"preprocessing_failed: {e}",
                region=region,
            )

        # 4. OCR text extraction with confidence estimation
        extracted_text = ""
        confidence = 0.0
        engine_reason = "ocr_success"

        if ocr_engine is not None:
            try:
                extracted_text, confidence = ocr_engine(preprocessed_img)
            except Exception as e:
                return OCRResult(
                    text="",
                    normalized_text="",
                    confidence=0.0,
                    status="UNKNOWN",
                    classification="UNKNOWN",
                    reason=f"ocr_engine_error: {e}",
                    region=region,
                )
        else:
            # Try pytesseract if available
            try:
                import pytesseract
                data = pytesseract.image_to_data(preprocessed_img, output_type=pytesseract.Output.DICT)
                n_boxes = len(data.get("text", []))
                words = []
                confs = []
                for i in range(n_boxes):
                    w = data["text"][i].strip()
                    c = int(data.get("conf", [0])[i])
                    if w and c > 0:
                        words.append(w)
                        confs.append(c / 100.0)
                if words:
                    extracted_text = " ".join(words)
                    confidence = sum(confs) / len(confs)
                else:
                    extracted_text = ""
                    confidence = 0.0
                    engine_reason = "no_text_detected"
            except (ImportError, Exception):
                return OCRResult(
                    text="",
                    normalized_text="",
                    confidence=0.0,
                    status="UNKNOWN",
                    classification="UNKNOWN",
                    reason="ocr_engine_unavailable",
                    region=region,
                )

        # 5. Text normalization
        normalized = self.normalize_text(extracted_text)
        if not normalized:
            return OCRResult(
                text="",
                normalized_text="",
                confidence=0.0,
                status="UNKNOWN",
                classification="UNKNOWN",
                reason=engine_reason if engine_reason != "ocr_success" else "blank_ocr_result",
                region=region,
                is_blank=True,
            )

        # 6. Confidence gating
        if confidence < self.confidence_threshold:
            return OCRResult(
                text=extracted_text,
                normalized_text=normalized,
                confidence=round(confidence, 4),
                status="UNKNOWN",
                classification="UNKNOWN",
                reason=f"low_confidence: {confidence:.2f} < threshold {self.confidence_threshold:.2f}",
                region=region,
            )

        # 7. Classification against known UI patterns
        status = "UNKNOWN"
        classification = "UNKNOWN"
        reason = "unrecognized_ui"

        if any(p in normalized for p in ("suspicious activity", "confirm your info", "checkpoint", "help us confirm")):
            status = "FAIL"
            classification = "CHALLENGE_REQUIRED"
            reason = "security_checkpoint_detected"
        elif any(p in normalized for p in ("sorry this page isn t available", "page isn t available", "link may be broken")):
            status = "FAIL"
            classification = "PROFILE_NOT_FOUND"
            reason = "profile_not_found"
        elif any(p in normalized for p in ("log in to instagram", "switch accounts", "log in")):
            status = "FAIL"
            classification = "LOGIN_REQUIRED"
            reason = "login_screen_detected"
        elif any(p in normalized for p in ("you can t message this account", "cannot message", "restricted profile")):
            status = "FAIL"
            classification = "DM_NOT_AVAILABLE"
            reason = "dm_restricted"
        elif target_phrases:
            for phrase in target_phrases:
                norm_phrase = self.normalize_text(phrase)
                if norm_phrase and norm_phrase in normalized:
                    status = "PASS"
                    classification = "SUCCESS"
                    reason = f"matched_target_phrase: '{phrase}'"
                    break
        elif any(p in normalized for p in ("message sent", "type a message", "sent", "following", "message")):
            status = "PASS"
            classification = "SUCCESS"
            reason = "standard_ui_signal_confirmed"

        return OCRResult(
            text=extracted_text,
            normalized_text=normalized,
            confidence=round(confidence, 4),
            status=status,
            classification=classification,
            reason=reason,
            region=region,
        )


class StandardVisionService(VisionService):
    """
    Production Vision Service providing OCR with confidence estimation and deterministic fallback:
    DOM extraction -> if insufficient -> OCR/vision -> confidence -> verification gate.
    Never allows raw OCR output to directly authorize sending without passing the verification engine.
    """

    def __init__(self, confidence_threshold: float = 0.85):
        self.confidence_threshold = confidence_threshold
        self.pipeline = VisionPipeline(confidence_threshold=confidence_threshold)

    def extract_text(self, image_path: str, region: Optional[Tuple[int, int, int, int]] = None) -> str:
        res = self.pipeline.process(image_path=image_path, region=region)
        if res.confidence < self.confidence_threshold:
            return ""
        return res.text

    def extract_with_confidence(
        self, image_path: str, region: Optional[Tuple[int, int, int, int]] = None
    ) -> Tuple[str, float, str]:
        """
        Extract text with bounded confidence score (0.0 to 1.0) and failure diagnosis.
        Returns: (extracted_text, confidence, reason)
        """
        res = self.pipeline.process(image_path=image_path, region=region)
        return res.text, res.confidence, res.reason

