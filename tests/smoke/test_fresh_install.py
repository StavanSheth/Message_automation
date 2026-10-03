"""Smoke test verifying clean installation, migrations, backend bootstrap, dashboard, and browser launch."""

import pytest
import os
from pathlib import Path

from backend.browser.runtime import validate_runtime
from backend.browser.driver import PlaywrightBrowserDriver
from backend.browser.browser_types import BrowserLaunchConfig
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.bootstrap import build_production_app
from backend.config.settings import AppSettings
from backend.web.server import DashboardServer


class TestFreshInstallationSmoke:
    """Smoke test suite verifying that a fresh environment is operational."""

    def test_imports_and_dependencies(self):
        """Verify all critical dependencies import without ImportError or circular dependency."""
        import playwright
        import PIL
        import sqlite3
        import urllib.parse
        assert playwright is not None
        assert PIL is not None
        assert sqlite3 is not None

    def test_playwright_runtime_smoke(self):
        """Verify Playwright runtime detection and real launch capability."""
        diag = validate_runtime(perform_smoke_test=True)
        assert diag.playwright_installed is True
        assert diag.executable_exists is True
        assert diag.can_launch is True
        assert diag.smoke_test_passed is True

    def test_fresh_database_initialization_and_migration(self, tmp_path):
        """Verify database initialization and migration on a completely fresh path."""
        db_path = str(tmp_path / "fresh_test.db")
        db_mgr = DatabaseManager(db_path=db_path)

        runner = MigrationRunner(db_mgr)
        applied = runner.apply_pending()
        assert len(applied) > 0

        # Verify tables exist
        conn = db_mgr.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {row[0] for row in cursor.fetchall()}
        assert "contacts" in tables
        assert "tasks" in tables
        assert "messages" in tables
        assert "schema_migrations" in tables

    def test_app_bootstrap_and_dashboard_startup(self, tmp_path):
        """Verify backend production bootstrap and dashboard initialization."""
        db_path = str(tmp_path / "app_smoke.db")
        settings = AppSettings(
            database_path=db_path,
            browser_headless=True,
        )
        app = build_production_app(db_path=db_path, settings=settings)
        is_valid, errors = app.validate_dependency_graph()
        assert is_valid is True
        assert len(errors) == 0

        # Start app
        start_res = app.start()
        assert start_res["status"] == "ok"

        # Check dashboard server can bind and respond
        server = DashboardServer(host="127.0.0.1", port=0, app_state=app)
        url = server.start()
        try:
            assert server.port > 0
            assert url.startswith("http://127.0.0.1:")
            assert server.is_healthy() is True
        finally:
            server.stop()
            app.stop()

    def test_browser_launch_navigate_and_close(self, tmp_path):
        """Verify real browser launch, navigate, screenshot, and clean termination."""
        profile_dir = tmp_path / "smoke_browser_profile"
        profile_dir.mkdir(parents=True, exist_ok=True)

        config = BrowserLaunchConfig(
            headless=True,
            profile_directory=str(profile_dir),
        )
        driver = PlaywrightBrowserDriver(config)
        driver.initialize()
        driver.launch()
        assert driver.pid is not None
        assert driver.pid > 0
        assert driver.is_alive() is True
        assert driver.is_process_alive() is True

        try:
            loaded = driver.navigate("about:blank")
            assert "about:blank" in loaded
            result = driver.evaluate("1 + 1")
            assert result == 2
            screenshot_path = str(tmp_path / "test_shot.png")
            driver.screenshot(screenshot_path)
            assert os.path.exists(screenshot_path)
            assert os.path.getsize(screenshot_path) > 0
        finally:
            driver.close()

        assert driver.is_alive() is False
        assert driver.is_process_alive() is False
