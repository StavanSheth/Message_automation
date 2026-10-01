"""Unit tests for BrowserManager concurrency, mode fallback, session lifecycle, and profile isolation."""

import pytest
from unittest.mock import MagicMock

from backend.browser.manager import BrowserManager
from backend.browser.browser_types import SessionStatus
from backend.browser.exceptions import BrowserSessionError
from backend.config.settings import AppSettings, reset_settings, set_settings
from backend.domain.models import HardwareCapabilities
from backend.domain.enums import WorkerMode, ErrorCode
from backend.browser.driver import BrowserDriver
from tests.fixtures.mock_browser import MockBrowserDriver


@pytest.fixture(autouse=True)
def clean_env():
    reset_settings()
    yield
    reset_settings()


def make_hardware(
    multi_available=True,
    recommended=4,
    gpu_avail=True,
    vram=4.0,
    total_ram=16.0,
    avail_ram=12.0,
):
    return HardwareCapabilities(
        cpu_count=8,
        total_ram_gb=total_ram,
        available_ram_gb=avail_ram,
        gpu_available=gpu_avail,
        gpu_vendor="NVIDIA" if gpu_avail else None,
        gpu_name="RTX 3080" if gpu_avail else None,
        vram_gb=vram,
        single_browser_available=True,
        multi_browser_available=multi_available,
        recommended_max_workers=recommended,
    )


def test_browser_manager_single_mode_defaults(tmp_path):
    settings = AppSettings(worker_mode="single_browser", max_workers=5, browser_profile_directory=str(tmp_path))
    hw = make_hardware(multi_available=True, recommended=4)
    bm = BrowserManager(settings=settings, hardware_capabilities=hw)

    assert bm.effective_mode == WorkerMode.SINGLE_BROWSER
    assert bm.effective_max_workers == 1


def test_browser_manager_multi_mode_sufficient_hardware(tmp_path):
    settings = AppSettings(worker_mode="multi_browser", max_workers=3, browser_profile_directory=str(tmp_path))
    hw = make_hardware(multi_available=True, recommended=4)
    bm = BrowserManager(settings=settings, hardware_capabilities=hw)

    assert bm.effective_mode == WorkerMode.MULTI_BROWSER
    assert bm.effective_max_workers == 3


def test_browser_manager_multi_mode_insufficient_gpu(tmp_path):
    settings = AppSettings(worker_mode="multi_browser", max_workers=4, browser_profile_directory=str(tmp_path))
    # No qualifying GPU
    hw = make_hardware(multi_available=False, gpu_avail=False, vram=0.0)
    bm = BrowserManager(settings=settings, hardware_capabilities=hw)

    assert bm.effective_mode == WorkerMode.SINGLE_BROWSER
    assert bm.effective_max_workers == 1


def test_browser_manager_multi_mode_insufficient_ram(tmp_path):
    settings = AppSettings(worker_mode="multi_browser", max_workers=4, browser_profile_directory=str(tmp_path))
    # Qualifying GPU but insufficient RAM (< 4GB)
    hw = make_hardware(multi_available=True, total_ram=2.0, avail_ram=1.0)
    bm = BrowserManager(settings=settings, hardware_capabilities=hw)

    assert bm.effective_mode == WorkerMode.SINGLE_BROWSER
    assert bm.effective_max_workers == 1


def test_browser_manager_max_workers_bounded_by_hardware(tmp_path):
    settings = AppSettings(worker_mode="multi_browser", max_workers=10, browser_profile_directory=str(tmp_path))
    # Hardware only supports 2
    hw = make_hardware(multi_available=True, recommended=2)
    bm = BrowserManager(settings=settings, hardware_capabilities=hw)

    assert bm.effective_mode == WorkerMode.MULTI_BROWSER
    assert bm.effective_max_workers == 2


def test_browser_manager_max_workers_one_in_multi_mode(tmp_path):
    settings = AppSettings(worker_mode="multi_browser", max_workers=1, browser_profile_directory=str(tmp_path))
    hw = make_hardware(multi_available=True, recommended=4)
    bm = BrowserManager(settings=settings, hardware_capabilities=hw)

    assert bm.effective_mode == WorkerMode.MULTI_BROWSER
    assert bm.effective_max_workers == 1


def test_create_and_reuse_session(tmp_path):
    settings = AppSettings(worker_mode="single_browser", browser_profile_directory=str(tmp_path))
    driver = MockBrowserDriver()
    bm = BrowserManager(settings=settings, driver_factory=lambda: driver)

    sess1 = bm.create_session(worker_id="w-1")
    sess1.start()
    assert sess1.is_alive()

    # Reusing same worker_id returns same session
    sess2 = bm.create_session(worker_id="w-1")
    assert sess2.session_id == sess1.session_id


def test_max_concurrent_sessions_enforced(tmp_path):
    settings = AppSettings(worker_mode="single_browser", browser_profile_directory=str(tmp_path))
    driver = MockBrowserDriver()
    bm = BrowserManager(settings=settings, driver_factory=lambda: driver)

    sess1 = bm.create_session(worker_id="w-1")
    sess1.start()

    with pytest.raises(BrowserSessionError, match="Maximum concurrent browser sessions"):
        bm.create_session(worker_id="w-2")


def test_profile_isolation_between_workers(tmp_path):
    settings = AppSettings(worker_mode="multi_browser", max_workers=2, browser_profile_directory=str(tmp_path))
    hw = make_hardware(multi_available=True, recommended=2)
    bm = BrowserManager(settings=settings, hardware_capabilities=hw, driver_factory=MockBrowserDriver)

    sess1 = bm.create_session(worker_id="w-1", profile_name="shared_prof")
    sess1.start()

    # Worker 2 attempting to steal worker 1's profile must be rejected
    with pytest.raises(BrowserSessionError, match="Browser profiles cannot be shared"):
        bm.create_session(worker_id="w-2", profile_name="shared_prof")


def test_start_session_failure_cleans_phantom(tmp_path):
    settings = AppSettings(worker_mode="single_browser", browser_profile_directory=str(tmp_path))
    failing_driver = MockBrowserDriver()
    failing_driver.launch = MagicMock(side_effect=RuntimeError("Launch crash"))

    bm = BrowserManager(settings=settings, driver_factory=lambda: failing_driver)
    sess = bm.create_session(worker_id="w-fail")

    with pytest.raises(Exception):
        bm.start_session(sess.session_id)

    # Must be cleaned up, not left as a phantom session
    assert bm.get_session(sess.session_id) is None
    assert bm.get_session_for_worker("w-fail") is None


def test_idempotent_shutdown(tmp_path):
    settings = AppSettings(worker_mode="single_browser", browser_profile_directory=str(tmp_path))
    bm = BrowserManager(settings=settings, driver_factory=MockBrowserDriver)
    sess = bm.create_session(worker_id="w-1")
    sess.start()

    bm.shutdown()
    assert len(bm._active_sessions) == 0
    # Second shutdown should be safe
    bm.shutdown()
