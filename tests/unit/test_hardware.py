"""Unit tests for hardware detection and worker recommendation rules."""

import pytest
from backend.health.hardware import HardwareDetectionService, HardwareInfoProvider


class MockHardwareProvider:
    def __init__(self, cpu: int, ram: tuple[float, float], gpu: tuple[bool, str, str, float]):
        self._cpu = cpu
        self._ram = ram
        self._gpu = gpu

    def get_cpu_count(self) -> int:
        return self._cpu

    def get_ram_info(self) -> tuple[float, float]:
        return self._ram

    def get_nvidia_gpu_info(self) -> tuple[bool, str, str, float]:
        return self._gpu


def test_nvidia_gpu_with_sufficient_vram_exposes_multi():
    # 8 cores, 16 GB RAM, NVIDIA RTX 3060 with 6 GB VRAM
    provider = MockHardwareProvider(
        cpu=8,
        ram=(16.0, 10.0),
        gpu=(True, "NVIDIA", "NVIDIA GeForce RTX 3060", 6.0),
    )
    detector = HardwareDetectionService(provider=provider)
    caps = detector.detect_capabilities(max_configured_workers=4)

    assert caps.gpu_available is True
    assert caps.gpu_vendor == "NVIDIA"
    assert caps.vram_gb == 6.0
    assert caps.single_browser_available is True
    assert caps.multi_browser_available is True
    assert caps.recommended_max_workers > 1


def test_nvidia_gpu_with_insufficient_vram_exposes_single_only():
    # 4 cores, 8 GB RAM, NVIDIA MX150 with 1.5 GB VRAM (< 2.0 GB)
    provider = MockHardwareProvider(
        cpu=4,
        ram=(8.0, 4.0),
        gpu=(True, "NVIDIA", "NVIDIA GeForce MX150", 1.5),
    )
    detector = HardwareDetectionService(provider=provider)
    caps = detector.detect_capabilities(max_configured_workers=4)

    assert caps.gpu_available is True
    assert caps.vram_gb == 1.5
    assert caps.single_browser_available is True
    assert caps.multi_browser_available is False  # Must NOT allow multi-browser mode
    assert caps.recommended_max_workers == 1


def test_no_nvidia_gpu_exposes_single_only():
    # 8 cores, 16 GB RAM, Integrated Intel Graphics
    provider = MockHardwareProvider(
        cpu=8,
        ram=(16.0, 10.0),
        gpu=(False, None, None, 0.0),
    )
    detector = HardwareDetectionService(provider=provider)
    caps = detector.detect_capabilities(max_configured_workers=4)

    assert caps.gpu_available is False
    assert caps.single_browser_available is True
    assert caps.multi_browser_available is False
    assert caps.recommended_max_workers == 1
