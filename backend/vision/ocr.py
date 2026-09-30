"""Vision and OCR engine contract."""

from abc import ABC, abstractmethod
from typing import Optional


class VisionService(ABC):
    """Contract for image processing and optical character recognition on screenshots."""

    @abstractmethod
    def extract_text(self, image_path: str, region: Optional[tuple[int, int, int, int]] = None) -> str:
        """Extract text from screenshot or specific bounding box."""
        pass
