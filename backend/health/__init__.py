"""Health package."""
from backend.health.hardware import HardwareDetectionService, HardwareInfoProvider, SystemHardwareInfoProvider
from backend.domain.models import HardwareCapabilities


def detect_hardware(max_configured_workers: int = 4) -> HardwareCapabilities:
    """Convenience function for one-shot hardware detection."""
    return HardwareDetectionService().detect_capabilities(max_configured_workers)

