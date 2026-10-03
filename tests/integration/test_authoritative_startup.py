"""Integration tests for authoritative application startup, CLI, and health checks."""

import pytest
from unittest.mock import MagicMock, patch

from backend.config.settings import AppSettings
from backend.bootstrap import build_production_app
from backend.domain.enums import SystemState
from backend.cli import show_status, pause_app, resume_app, stop_app


class TestAuthoritativeStartup:
    def test_app_settings_visible_browser_default(self):
        s = AppSettings()
        s.validate()
        assert s.browser_headless is False
        assert s.application_mode == "MANUAL"

    def test_startup_health_check_and_dependency_graph(self, tmp_path):
        db_path = str(tmp_path / "startup_test.db")
        settings = AppSettings(
            database_path=db_path,
            browser_headless=True,
            execution_mode="MANUAL",
            application_mode="MANUAL",
        )
        app = build_production_app(db_path=db_path, settings=settings)

        # Validate dependency graph
        is_valid, errors = app.validate_dependency_graph()
        assert is_valid is True
        assert len(errors) == 0

        # Start app and verify RUNNING state reached
        res = app.start()
        assert res.get("status") == "ok"
        assert app.control_service.state == SystemState.RUNNING

        # Pause and Resume
        assert app.pause("test pause") is True
        assert app.control_service.state == SystemState.PAUSED
        assert app.resume("test resume") is True
        assert app.control_service.state == SystemState.RUNNING

        # Clean graceful shutdown
        app.stop()
        assert app.control_service.state == SystemState.STOPPED

    def test_cli_status_command(self, tmp_path, capsys):
        db_path = str(tmp_path / "cli_test.db")
        settings = AppSettings(database_path=db_path, browser_headless=True)
        app = build_production_app(db_path=db_path, settings=settings)
        app.start()

        # Run show_status
        show_status(db_path=db_path)
        captured = capsys.readouterr().out
        assert "MESSAGE_AUTOMATION — SYSTEM STATUS" in captured
        assert "Application State:" in captured
        assert "Database:" in captured

        app.stop()
