"""Phase 2 unit tests: browser types, session, profiles, health, driver, URL validators, worker, execution context."""

import pytest
import time
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta

from backend.config.settings import AppSettings, reset_settings, set_settings
from backend.browser.browser_types import (
    BrowserType, BrowserStatus, SessionStatus, PageStatus,
    BrowserLaunchConfig, BrowserHealthResult, BrowserSessionInfo,
)
from backend.browser.exceptions import (
    BrowserException, BrowserLaunchError, BrowserCrashError,
    BrowserTimeoutError, BrowserNavigationError, BrowserSessionError,
)
from backend.browser.session import BrowserSessionInstance
from backend.browser.health import BrowserHealthChecker
from backend.browser.lifecycle import BrowserLifecycleManager
from backend.domain.enums import ErrorCode, WorkerMode, WorkerStatus, EventCode
from backend.domain.errors import ValidationError
from backend.automation.execution_context import ExecutionContext
from backend.events.correlation import (
    generate_id, get_correlation_id, set_correlation_id, clear_correlation_id, reset_counters,
)
from backend.sources.browser_sheet.validators import (
    validate_spreadsheet_url, UrlValidationResult, map_validation_to_access_status,
)
from backend.sources.browser_sheet.spreadsheet import SpreadsheetStructureValidator
from backend.domain.enums import SourceAccessStatus

from tests.fixtures.mock_browser import MockBrowserDriver, create_mock_session


@pytest.fixture(autouse=True)
def clean_settings_and_counters():
    reset_settings()
    reset_counters()
    yield
    reset_settings()
    reset_counters()


# ── Browser Types ──────────────────────────────────────────────────────────

class TestBrowserTypes:
    def test_browser_type_enum_values(self):
        assert BrowserType.CHROMIUM.value == "chromium"
        assert BrowserType.CHROME.value == "chrome"
        assert BrowserType.FIREFOX.value == "firefox"

    def test_session_status_enum(self):
        assert SessionStatus.READY.value == "READY"
        assert SessionStatus.CRASHED.value == "CRASHED"

    def test_launch_config_defaults(self):
        cfg = BrowserLaunchConfig()
        assert cfg.browser_type == BrowserType.CHROMIUM
        assert cfg.headless is True
        assert cfg.timeout_seconds == 30
        assert cfg.viewport_width == 1280

    def test_health_result_dataclass(self):
        hr = BrowserHealthResult(
            healthy=True, browser_connected=True,
            context_available=True, page_available=True,
            latency_ms=5.0,
        )
        assert hr.healthy is True
        assert hr.latency_ms == 5.0


# ── Browser Exceptions ────────────────────────────────────────────────────

class TestBrowserExceptions:
    def test_browser_launch_error(self):
        err = BrowserLaunchError("launch failed")
        assert err.code == ErrorCode.BROWSER_CRASH
        assert err.retryable is True

    def test_browser_timeout_error(self):
        err = BrowserTimeoutError("timed out")
        assert err.code == ErrorCode.TIMEOUT
        assert err.retryable is True

    def test_browser_session_error(self):
        err = BrowserSessionError("session broken")
        assert err.code == ErrorCode.SESSION_EXPIRED
        assert err.retryable is False


# ── Mock Browser Driver ───────────────────────────────────────────────────

class TestMockBrowserDriver:
    def test_launch_and_connect(self):
        driver = MockBrowserDriver()
        assert not driver.is_connected()
        driver.launch()
        assert driver.is_connected()

    def test_navigate_tracks_url(self):
        driver = MockBrowserDriver()
        driver.launch()
        result = driver.navigate("https://example.com")
        assert result == "https://example.com"
        assert driver.current_url() == "https://example.com"
        assert driver.navigate_history == ["https://example.com"]

    def test_close_disconnects(self):
        driver = MockBrowserDriver()
        driver.launch()
        driver.close()
        assert not driver.is_connected()

    def test_evaluate_with_preconfigured_result(self):
        driver = MockBrowserDriver()
        driver.launch()
        driver.set_evaluate_result("1+1", 2)
        assert driver.evaluate("1+1") == 2


# ── Browser Session ───────────────────────────────────────────────────────

class TestBrowserSession:
    def test_session_start_and_stop(self):
        session = create_mock_session(auto_start=False)
        assert session.status == SessionStatus.NOT_STARTED
        session.start()
        assert session.status == SessionStatus.READY
        assert session.is_alive()
        session.stop()
        assert session.status == SessionStatus.STOPPED

    def test_session_navigate(self):
        session = create_mock_session()
        url = session.navigate("https://docs.google.com/spreadsheets/d/test")
        assert url == "https://docs.google.com/spreadsheets/d/test"
        assert session.current_url == url

    def test_session_health_check_healthy(self):
        session = create_mock_session()
        result = session.health_check()
        assert result.healthy is True
        assert result.browser_connected is True

    def test_session_health_check_after_stop(self):
        session = create_mock_session()
        session.stop()
        result = session.health_check()
        assert result.healthy is False

    def test_session_restart(self):
        session = create_mock_session()
        session.restart()
        assert session.status == SessionStatus.READY
        assert session.is_alive()

    def test_session_to_info(self):
        session = create_mock_session(session_id="s1", worker_id="w1")
        info = session.to_info()
        assert info.session_id == "s1"
        assert info.worker_id == "w1"
        assert info.status == SessionStatus.READY

    def test_navigate_crashed_session_raises(self):
        session = create_mock_session()
        session.stop()
        with pytest.raises(BrowserCrashError):
            session.navigate("https://example.com")


# ── Browser Health Checker ─────────────────────────────────────────────────

class TestBrowserHealthChecker:
    def test_check_healthy_session(self):
        session = create_mock_session()
        result = BrowserHealthChecker.check_session(session)
        assert result.healthy is True

    def test_check_none_session(self):
        result = BrowserHealthChecker.check_session(None)
        assert result.healthy is False
        assert result.error_code == ErrorCode.SOURCE_UNAVAILABLE.value


# ── Browser Lifecycle Manager ──────────────────────────────────────────────

class TestBrowserLifecycleManager:
    def test_register_and_stop_session(self):
        lm = BrowserLifecycleManager()
        session = create_mock_session(session_id="lifecycle-1")
        lm.register_session(session)
        lm.stop_session("lifecycle-1")
        assert session.status == SessionStatus.STOPPED

    def test_cleanup_all(self):
        lm = BrowserLifecycleManager()
        s1 = create_mock_session(session_id="lm-1")
        s2 = create_mock_session(session_id="lm-2")
        lm.register_session(s1)
        lm.register_session(s2)
        lm.cleanup_all()
        assert s1.status == SessionStatus.STOPPED
        assert s2.status == SessionStatus.STOPPED


# ── Browser Profiles ───────────────────────────────────────────────────────

class TestBrowserProfiles:
    def test_create_profile(self, tmp_path):
        settings = AppSettings(browser_profile_directory=str(tmp_path / "profiles"))
        set_settings(settings)
        from backend.browser.profiles import BrowserProfileManager
        pm = BrowserProfileManager(str(tmp_path / "profiles"))
        profile = pm.create_or_get_profile("test_profile")
        assert profile.profile_id == "prof_test_profile"
        assert profile.status == "ACTIVE"
        assert (tmp_path / "profiles" / "test_profile").is_dir()

    def test_get_existing_profile(self, tmp_path):
        settings = AppSettings(browser_profile_directory=str(tmp_path / "profiles"))
        set_settings(settings)
        from backend.browser.profiles import BrowserProfileManager
        pm = BrowserProfileManager(str(tmp_path / "profiles"))
        p1 = pm.create_or_get_profile("reuse")
        p2 = pm.create_or_get_profile("reuse")
        assert p1.profile_id == p2.profile_id

    def test_invalid_profile_name_raises(self, tmp_path):
        settings = AppSettings(browser_profile_directory=str(tmp_path / "profiles"))
        set_settings(settings)
        from backend.browser.profiles import BrowserProfileManager
        pm = BrowserProfileManager(str(tmp_path / "profiles"))
        with pytest.raises(ValidationError):
            pm.create_or_get_profile("   ")


# ── URL Validators ─────────────────────────────────────────────────────────

class TestUrlValidators:
    def test_valid_google_sheets_url(self):
        result, err = validate_spreadsheet_url(
            "https://docs.google.com/spreadsheets/d/1234/edit"
        )
        assert result == UrlValidationResult.VALID_SOURCE
        assert err is None

    def test_valid_excel_online_url(self):
        result, err = validate_spreadsheet_url(
            "https://onedrive.live.com/view.aspx?resid=ABC"
        )
        assert result == UrlValidationResult.VALID_SOURCE

    def test_http_rejected_for_non_local(self):
        result, err = validate_spreadsheet_url(
            "http://docs.google.com/spreadsheets/d/123"
        )
        assert result == UrlValidationResult.INVALID_URL
        assert "HTTPS" in err

    def test_http_allowed_for_localhost(self):
        result, err = validate_spreadsheet_url("http://localhost:8080/sheet")
        assert result == UrlValidationResult.VALID_SOURCE

    def test_unsupported_domain(self):
        result, err = validate_spreadsheet_url("https://random-site.org/sheet")
        assert result == UrlValidationResult.UNSUPPORTED_SOURCE

    def test_empty_url(self):
        result, err = validate_spreadsheet_url("")
        assert result == UrlValidationResult.INVALID_URL

    def test_missing_scheme(self):
        result, err = validate_spreadsheet_url("docs.google.com/spreadsheets/d/1")
        assert result == UrlValidationResult.INVALID_URL

    def test_google_docs_non_spreadsheet_rejected(self):
        result, err = validate_spreadsheet_url(
            "https://docs.google.com/document/d/1234"
        )
        assert result == UrlValidationResult.UNSUPPORTED_SOURCE

    def test_map_validation_to_access_status(self):
        assert map_validation_to_access_status(UrlValidationResult.VALID_SOURCE) == SourceAccessStatus.ACCESSIBLE
        assert map_validation_to_access_status(UrlValidationResult.INVALID_URL) == SourceAccessStatus.UNSUPPORTED_STRUCTURE


# ── Spreadsheet Structure Validator ────────────────────────────────────────

class TestSpreadsheetStructureValidator:
    def test_validate_valid_headers(self):
        headers = ["Name", "Instagram URL", "Message", "Replied"]
        canonical, idx_map = SpreadsheetStructureValidator.validate_headers(headers)
        assert "instagram_url" in canonical
        assert "message" in canonical

    def test_missing_required_field_raises(self):
        headers = ["Name", "Notes"]
        with pytest.raises(ValidationError, match="mandatory required"):
            SpreadsheetStructureValidator.validate_headers(headers)

    def test_empty_headers_raises(self):
        with pytest.raises(ValidationError, match="empty"):
            SpreadsheetStructureValidator.validate_headers([])

    def test_parse_row_basic(self):
        headers = ["Name", "Instagram URL", "Message"]
        _, idx_map = SpreadsheetStructureValidator.validate_headers(headers)
        row = SpreadsheetStructureValidator.parse_row(
            ["John", "https://instagram.com/john", "Hi there"],
            row_index=2,
            idx_to_canonical=idx_map,
        )
        assert row is not None
        assert row.name == "John"
        assert row.instagram_url == "https://instagram.com/john"
        assert row.message == "Hi there"
        assert row.checksum != ""

    def test_parse_empty_row_returns_none(self):
        headers = ["Name", "Instagram URL", "Message"]
        _, idx_map = SpreadsheetStructureValidator.validate_headers(headers)
        row = SpreadsheetStructureValidator.parse_row(
            ["", "", ""],
            row_index=2,
            idx_to_canonical=idx_map,
        )
        assert row is None


# ── Execution Context ──────────────────────────────────────────────────────

class TestExecutionContext:
    def test_context_generates_ids(self):
        ctx = ExecutionContext()
        assert ctx.run_id.startswith("RUN-")
        assert ctx.correlation_id.startswith("CORR-")

    def test_context_sets_global_correlation(self):
        clear_correlation_id()
        ctx = ExecutionContext()
        assert get_correlation_id() == ctx.correlation_id

    def test_context_to_dict(self):
        ctx = ExecutionContext(task_id="t1", worker_id="w1")
        d = ctx.to_dict()
        assert d["task_id"] == "t1"
        assert d["worker_id"] == "w1"
        assert "run_id" in d


# ── Correlation ID Helpers ─────────────────────────────────────────────────

class TestCorrelationHelpers:
    def test_set_and_get_correlation_id(self):
        clear_correlation_id()
        assert get_correlation_id() is None
        set_correlation_id("test-corr-1")
        assert get_correlation_id() == "test-corr-1"
        clear_correlation_id()
        assert get_correlation_id() is None


# ── Settings Phase 2 Fields ───────────────────────────────────────────────

class TestPhase2Settings:
    def test_browser_settings_defaults(self):
        s = AppSettings()
        s.validate()
        assert s.browser_type == "chromium"
        assert s.browser_headless is True
        assert s.browser_profile_directory == "data/browser_profiles"
        assert s.browser_startup_timeout == 30
        assert s.worker_heartbeat_interval == 15
        assert s.worker_stale_timeout == 60

    def test_invalid_browser_type_raises(self):
        with pytest.raises(ValidationError, match="browser_type"):
            AppSettings(browser_type="netscape").validate()

    def test_invalid_browser_startup_timeout_raises(self):
        with pytest.raises(ValidationError, match="browser_startup_timeout"):
            AppSettings(browser_startup_timeout=0).validate()

    def test_bool_coercion_from_string(self):
        s = AppSettings.from_dict({"browser_headless": "false"})
        assert s.browser_headless is False
        s2 = AppSettings.from_dict({"browser_headless": "true"})
        assert s2.browser_headless is True


# ── Worker (unit-level, no DB) ─────────────────────────────────────────────

class TestWorkerUnit:
    def test_worker_heartbeat(self):
        from backend.workers.worker import Worker
        mock_task_repo = MagicMock()
        mock_event_repo = MagicMock()
        w = Worker(
            worker_id="w1", worker_code="worker-1",
            mode=WorkerMode.SINGLE_BROWSER,
            task_repo=mock_task_repo, event_repo=mock_event_repo,
            stale_timeout=1,
        )
        hb1 = w.heartbeat()
        assert hb1 is not None
        assert not w.is_stale()

    def test_worker_start_stop(self):
        from backend.workers.worker import Worker
        mock_task_repo = MagicMock()
        mock_event_repo = MagicMock()
        mock_event_repo.record = MagicMock()
        w = Worker(
            worker_id="w2", worker_code="worker-2",
            mode=WorkerMode.SINGLE_BROWSER,
            task_repo=mock_task_repo, event_repo=mock_event_repo,
        )
        w.start()
        assert w.status == WorkerStatus.IDLE
        w.stop()
        assert w.status == WorkerStatus.STOPPED

    def test_worker_claim_task(self):
        from backend.workers.worker import Worker
        mock_task_repo = MagicMock()
        mock_task_repo.claim_task.return_value = True
        mock_event_repo = MagicMock()
        mock_event_repo.record = MagicMock()
        w = Worker(
            worker_id="w3", worker_code="worker-3",
            mode=WorkerMode.SINGLE_BROWSER,
            task_repo=mock_task_repo, event_repo=mock_event_repo,
        )
        w.start()
        assert w.claim_task("task-1")
        assert w.status == WorkerStatus.BUSY
        assert w.current_task_id == "task-1"

    def test_worker_to_record(self):
        from backend.workers.worker import Worker
        mock_task_repo = MagicMock()
        mock_event_repo = MagicMock()
        w = Worker(
            worker_id="w4", worker_code="worker-4",
            mode=WorkerMode.SINGLE_BROWSER,
            task_repo=mock_task_repo, event_repo=mock_event_repo,
        )
        rec = w.to_record()
        assert rec.id == "w4"
        assert rec.worker_code == "worker-4"
        assert rec.mode == WorkerMode.SINGLE_BROWSER


# ── Worker Health Monitor ──────────────────────────────────────────────────

class TestWorkerHealthMonitor:
    def test_check_healthy_worker(self):
        from backend.workers.worker import Worker
        from backend.workers.worker_health import WorkerHealthMonitor
        mock_task_repo = MagicMock()
        mock_event_repo = MagicMock()
        w = Worker(
            worker_id="wh1", worker_code="wh-1",
            mode=WorkerMode.SINGLE_BROWSER,
            task_repo=mock_task_repo, event_repo=mock_event_repo,
        )
        w.start()
        result = WorkerHealthMonitor.check_worker(w)
        assert result["healthy"] is True
        assert result["is_stale"] is False

    def test_find_stale_workers(self):
        from backend.workers.worker import Worker
        from backend.workers.worker_health import WorkerHealthMonitor
        mock_task_repo = MagicMock()
        mock_event_repo = MagicMock()
        w = Worker(
            worker_id="stale1", worker_code="stale-1",
            mode=WorkerMode.SINGLE_BROWSER,
            task_repo=mock_task_repo, event_repo=mock_event_repo,
            stale_timeout=0,  # immediately stale
        )
        w.start()
        # Force heartbeat to old time
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        w.last_heartbeat = old_time
        stale = WorkerHealthMonitor.find_stale_workers([w])
        assert len(stale) == 1
        assert stale[0].worker_id == "stale1"
