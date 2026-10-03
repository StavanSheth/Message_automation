"""Integration test for visible browser launch, lifecycle, and profile isolation."""

import pytest
from pathlib import Path
from backend.browser.driver import PlaywrightBrowserDriver, find_browser_executable
from backend.browser.browser_types import BrowserLaunchConfig, BrowserType, SessionStatus
from backend.browser.manager import BrowserManager
from backend.browser.profiles import BrowserProfileManager
from backend.config.settings import AppSettings


class TestVisibleBrowserLifecycle:
    def test_find_browser_executable(self):
        # Should return a string or None without raising an exception
        path = find_browser_executable()
        if path:
            assert Path(path).exists()

    def test_deterministic_driver_startup(self, tmp_path):
        profile_dir = str(tmp_path / "prof_deterministic")
        cfg = BrowserLaunchConfig(
            browser_type=BrowserType.CHROMIUM,
            headless=True,  # headless for fast CI integration
            profile_directory=profile_dir,
        )
        driver = PlaywrightBrowserDriver(cfg)
        driver.initialize()
        driver.launch()
        assert driver.is_connected() is True
        assert driver.verify_alive() is True

        # Test tab creation and closure
        driver.create_page()
        driver.close_page()

        driver.close()
        assert driver.is_connected() is False

    def test_profile_isolation_and_account_binding(self, tmp_path):
        base_dir = str(tmp_path / "profiles")
        prof_mgr = BrowserProfileManager(base_dir)

        # Profile 1 for account_A
        prof_a = prof_mgr.create_or_get_profile("account_A")
        assert prof_mgr.validate_profile_usability(prof_a.profile_id) is True

        # Same account returns same profile
        prof_a2 = prof_mgr.create_or_get_profile("account_A")
        assert prof_a.profile_path == prof_a2.profile_path

        # Different account returns different profile
        prof_b = prof_mgr.create_or_get_profile("account_B")
        assert prof_a.profile_path != prof_b.profile_path

        # Auto-discovery persists across restarts
        prof_mgr2 = BrowserProfileManager(base_dir)
        discovered = prof_mgr2.list_profiles()
        discovered_ids = {p.profile_id for p in discovered}
        assert prof_a.profile_id in discovered_ids
        assert prof_b.profile_id in discovered_ids
