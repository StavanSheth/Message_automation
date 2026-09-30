"""Hardware capability detection service."""

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional, Protocol

from backend.domain.models import HardwareCapabilities


class HardwareInfoProvider(Protocol):
    """Protocol for querying system CPU, RAM, and GPU."""
    def get_cpu_count(self) -> int: ...
    def get_ram_info(self) -> tuple[float, float]: ...  # (total_gb, available_gb)
    def get_nvidia_gpu_info(self) -> tuple[bool, Optional[str], Optional[str], float]: ...  # (available, vendor, name, vram_gb)


class SystemHardwareInfoProvider:
    """Real system hardware inspector using standard library, ctypes, and nvidia-smi."""

    def get_cpu_count(self) -> int:
        return os.cpu_count() or 1

    def get_ram_info(self) -> tuple[float, float]:
        """Return (total_ram_gb, available_ram_gb)."""
        # On Windows, use ctypes GlobalMemoryStatusEx
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes

                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", wintypes.DWORD),
                        ("dwMemoryLoad", wintypes.DWORD),
                        ("ullTotalPhys", ctypes.c_uint64),
                        ("ullAvailPhys", ctypes.c_uint64),
                        ("ullTotalPageFile", ctypes.c_uint64),
                        ("ullAvailPageFile", ctypes.c_uint64),
                        ("ullTotalVirtual", ctypes.c_uint64),
                        ("ullAvailVirtual", ctypes.c_uint64),
                        ("sullAvailExtendedVirtual", ctypes.c_uint64),
                    ]

                stat = MEMORYSTATUSEX()
                stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
                if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                    total_gb = stat.ullTotalPhys / (1024 ** 3)
                    avail_gb = stat.ullAvailPhys / (1024 ** 3)
                    return round(total_gb, 2), round(avail_gb, 2)
            except Exception:
                pass

        # Fallback approximation
        return 8.0, 4.0

    def get_nvidia_gpu_info(self) -> tuple[bool, Optional[str], Optional[str], float]:
        """Detect NVIDIA GPU and VRAM via nvidia-smi command."""
        nvidia_smi = shutil.which("nvidia-smi")
        if not nvidia_smi:
            return False, None, None, 0.0

        try:
            cmd = [
                nvidia_smi,
                "--query-gpu=gpu_name,memory.total",
                "--format=csv,noheader,nounits",
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            if result.returncode == 0 and result.stdout.strip():
                line = result.stdout.strip().splitlines()[0]
                parts = [p.strip() for p in line.split(",")]
                gpu_name = parts[0]
                vram_mb = float(parts[1]) if len(parts) > 1 else 0.0
                vram_gb = round(vram_mb / 1024.0, 2)
                return True, "NVIDIA", gpu_name, vram_gb
        except Exception:
            pass

        return False, None, None, 0.0


class HardwareDetectionService:
    """Evaluates system hardware and determines browser execution modes and worker capacities."""

    def __init__(self, provider: Optional[HardwareInfoProvider] = None):
        self.provider = provider or SystemHardwareInfoProvider()

    def detect_capabilities(self, max_configured_workers: int = 4) -> HardwareCapabilities:
        """
        Inspect hardware and apply qualification rules:
        - NVIDIA GPU with >= 2 GB VRAM -> expose SINGLE and MULTI
        - No qualifying NVIDIA GPU -> expose SINGLE only
        - Multi worker count bounded by CPU, RAM, and VRAM
        """
        cpu_count = self.provider.get_cpu_count()
        total_ram, avail_ram = self.provider.get_ram_info()
        gpu_avail, gpu_vendor, gpu_name, vram_gb = self.provider.get_nvidia_gpu_info()

        # NVIDIA >= 2.0 GB VRAM requirement
        multi_available = bool(gpu_avail and gpu_vendor == "NVIDIA" and vram_gb >= 2.0)
        single_available = True

        # Calculate recommended max workers
        # Single mode: 1 worker
        # Multi mode: bounded by CPU (cpu_count - 1), available RAM (~1.5GB/worker), VRAM (~1GB/worker), and configured ceiling
        if multi_available:
            by_cpu = max(1, cpu_count - 1)
            by_ram = max(1, int(avail_ram / 1.5))
            by_vram = max(1, int(vram_gb / 1.0))
            recommended = min(by_cpu, by_ram, by_vram, max_configured_workers)
        else:
            recommended = 1

        return HardwareCapabilities(
            cpu_count=cpu_count,
            total_ram_gb=total_ram,
            available_ram_gb=avail_ram,
            gpu_available=gpu_avail,
            gpu_vendor=gpu_vendor,
            gpu_name=gpu_name,
            vram_gb=vram_gb,
            single_browser_available=single_available,
            multi_browser_available=multi_available,
            recommended_max_workers=recommended,
        )
