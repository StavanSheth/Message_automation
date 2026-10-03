"""Phase 2 unit tests: browser, session, profiles, health, lifecycle, driver, validators, workers, execution, recovery."""

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
from backend.domain.enums import ErrorCode, WorkerMode, WorkerStatus, EventCode, TaskState, TaskType, RepliedStatus
from backend.domain.errors import ValidationError
from backend.domain.models import Task, utc_now_iso
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
        assert cfg.headless is False  # Authoritative default matches AppSettings.browser_headless = False
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

    def test_browser_crash_error(self):
        err = BrowserCrashError("crashed")
        assert err.code == ErrorCode.BROWSER_CRASH
        assert err.retryable is True

    def test_browser_navigation_error(self):
        err = BrowserNavigationError("nav failed")
        assert err.code == ErrorCode.NETWORK_OFFLINE
        assert err.retryable is True


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

    def test_context_operations(self):
        driver = MockBrowserDriver()
        driver.launch()
        driver.new_context()
        assert driver._has_context
        driver.close_context()
        assert not driver._has_context

    def test_page_operations(self):
        driver = MockBrowserDriver()
        driver.launch()
        driver.new_page()
        assert driver._has_page
        driver.close_page()
        assert not driver._has_page


# ── Browser Session Lifecycle ─────────────────────────────────────────────

class TestBrowserSession:
    def test_session_start_and_stop(self):
        session = create_mock_session(auto_start=False)
        assert session.status == SessionStatus.NOT_STARTED
        session.start()
        assert session.status == SessionStatus.READY
        assert session.is_alive()
        session.stop()
        assert session.status == SessionStatus.STOPPED

    def test_session_lifecycle_states(self):
        """Verify NOT_STARTED → STARTING → READY → STOPPING → STOPPED"""
        session = create_mock_session(auto_start=False)
        assert session.status == SessionStatus.NOT_STARTED
        session.start()
        assert session.status == SessionStatus.READY
        session.stop()
        assert session.status == SessionStatus.STOPPED

    def test_session_navigate(self):
        session = create_mock_session()
        url = session.navigate("https://docs.google.com/spreadsheets/d/test")
        assert url == "https://docs.google.com/spreadsheets/d/test"
        assert session.current_url == url

    def test_session_navigate_updates_activity(self):
        session = create_mock_session()
        before = session.last_activity_at
        session.navigate("https://example.com")
        assert session.last_activity_at >= before

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

    def test_evaluate_on_stopped_session_raises(self):
        session = create_mock_session()
        session.stop()
        with pytest.raises(BrowserCrashError):
            session.evaluate("1+1")

    def test_double_stop_is_safe(self):
        session = create_mock_session()
        session.stop()
        session.stop()  # should not raise
        assert session.status == SessionStatus.STOPPED

    def test_session_crashed_state_detected(self):
        """If driver disconnects, health check marks session CRASHED."""
        session = create_mock_session()
        # Simulate driver disconnect
        session.driver._connected = False
        result = session.health_check()
        assert result.healthy is False
        assert session.status == SessionStatus.CRASHED


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

    def test_check_distinguishes_disconnect(self):
        session = create_mock_session()
        session.driver._connected = False
        result = BrowserHealthChecker.check_session(session)
        assert result.healthy is False


# ── Browser Lifecycle Manager ──────────────────────────────────────────────

class TestBrowserLifecycleManager:
    def test_register_and_stop_session(self):
        lm = BrowserLifecycleManager()
        session = create_mock_session(session_id="lifecycle-1")
        lm.register_session(session)
        lm.stop_session("lifecycle-1")
        assert session.status == SessionStatus.STOPPED

    def test_stop_unregisters_session(self):
        lm = BrowserLifecycleManager()
        session = create_mock_session(session_id="lm-unreg")
        lm.register_session(session)
        lm.stop_session("lm-unreg")
        assert "lm-unreg" not in lm._active_sessions

    def test_cleanup_all(self):
        lm = BrowserLifecycleManager()
        s1 = create_mock_session(session_id="lm-1")
        s2 = create_mock_session(session_id="lm-2")
        lm.register_session(s1)
        lm.register_session(s2)
        lm.cleanup_all()
        assert s1.status == SessionStatus.STOPPED
        assert s2.status == SessionStatus.STOPPED
        assert len(lm._active_sessions) == 0

    def test_recover_session(self):
        lm = BrowserLifecycleManager()
        session = create_mock_session(session_id="lm-crash")
        lm.register_session(session)
        session.driver._connected = False
        session.status = SessionStatus.CRASHED
        recovered = lm.recover_session("lm-crash")
        assert recovered.status == SessionStatus.READY
        assert recovered.is_alive()

    def test_double_stop_session_is_safe(self):
        lm = BrowserLifecycleManager()
        session = create_mock_session(session_id="lm-double")
        lm.register_session(session)
        lm.stop_session("lm-double")
        lm.stop_session("lm-double")  # should not raise


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

    def test_profile_traversal_protection(self, tmp_path):
        """Path traversal must be rejected."""
        settings = AppSettings(browser_profile_directory=str(tmp_path / "profiles"))
        set_settings(settings)
        from backend.browser.profiles import BrowserProfileManager
        pm = BrowserProfileManager(str(tmp_path / "profiles"))
        # ".." should be stripped to empty by sanitizer, raising ValidationError
        with pytest.raises(ValidationError):
            pm.create_or_get_profile("..")

    def test_profile_persistence_discovery(self, tmp_path):
        """Profiles on disk should be discoverable after app restart."""
        prof_dir = tmp_path / "profiles"
        (prof_dir / "existing_profile").mkdir(parents=True)
        settings = AppSettings(browser_profile_directory=str(prof_dir))
        set_settings(settings)
        from backend.browser.profiles import BrowserProfileManager
        pm = BrowserProfileManager(str(prof_dir))
        discovered = pm.discover_existing_profiles()
        assert len(discovered) == 1
        assert discovered[0].profile_id == "prof_existing_profile"

    def test_profile_deactivation(self, tmp_path):
        settings = AppSettings(browser_profile_directory=str(tmp_path / "profiles"))
        set_settings(settings)
        from backend.browser.profiles import BrowserProfileManager
        pm = BrowserProfileManager(str(tmp_path / "profiles"))
        pm.create_or_get_profile("deactivate_me")
        assert pm.deactivate_profile("prof_deactivate_me") is True
        assert pm.get_profile("prof_deactivate_me").status == "INACTIVE"


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

    def test_arbitrary_domain_allowed_by_default(self):
        result, err = validate_spreadsheet_url("https://random-site.org/sheet")
        assert result == UrlValidationResult.VALID_SOURCE
        assert err is None

    def test_unsupported_domain_with_whitelist(self):
        result, err = validate_spreadsheet_url("https://random-site.org/sheet", enforce_whitelist=True)
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

    def test_replied_status_parsing(self):
        headers = ["Name", "Instagram URL", "Message", "Replied"]
        _, idx_map = SpreadsheetStructureValidator.validate_headers(headers)
        row = SpreadsheetStructureValidator.parse_row(
            ["Test", "https://instagram.com/test", "msg", "YES"],
            row_index=2,
            idx_to_canonical=idx_map,
        )
        assert row is not None
        assert row.replied_status == RepliedStatus.YES


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
        assert s.browser_headless is False
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


# ── Worker Unit Tests ──────────────────────────────────────────────────────

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

    def test_worker_cannot_claim_while_busy(self):
        from backend.workers.worker import Worker
        mock_task_repo = MagicMock()
        mock_task_repo.claim_task.return_value = True
        mock_event_repo = MagicMock()
        w = Worker(
            worker_id="w-busy", worker_code="worker-busy",
            mode=WorkerMode.SINGLE_BROWSER,
            task_repo=mock_task_repo, event_repo=mock_event_repo,
        )
        w.start()
        w.claim_task("task-1")
        assert w.status == WorkerStatus.BUSY
        assert not w.claim_task("task-2")

    def test_worker_release_task(self):
        from backend.workers.worker import Worker
        mock_task_repo = MagicMock()
        mock_task_repo.claim_task.return_value = True
        mock_event_repo = MagicMock()
        w = Worker(
            worker_id="w-rel", worker_code="worker-rel",
            mode=WorkerMode.SINGLE_BROWSER,
            task_repo=mock_task_repo, event_repo=mock_event_repo,
        )
        w.start()
        w.claim_task("task-1")
        w.release_current_task()
        assert w.status == WorkerStatus.IDLE
        assert w.current_task_id is None

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
            stale_timeout=0,
        )
        w.start()
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        w.last_heartbeat = old_time
        stale = WorkerHealthMonitor.find_stale_workers([w])
        assert len(stale) == 1
        assert stale[0].worker_id == "stale1"


# ── DefaultWorkerManager ──────────────────────────────────────────────────

class TestDefaultWorkerManager:
    def test_max_worker_limit(self):
        from backend.workers.default_manager import DefaultWorkerManager
        s = AppSettings(max_workers=1)
        set_settings(s)
        mock_task_repo = MagicMock()
        mock_event_repo = MagicMock()
        mgr = DefaultWorkerManager(task_repo=mock_task_repo, event_repo=mock_event_repo)
        mgr.start_worker(WorkerMode.SINGLE_BROWSER)
        with pytest.raises(RuntimeError, match="max_workers"):
            mgr.start_worker(WorkerMode.SINGLE_BROWSER)

    def test_start_and_stop_worker(self):
        from backend.workers.default_manager import DefaultWorkerManager
        set_settings(AppSettings(max_workers=2))
        mock_task_repo = MagicMock()
        mock_event_repo = MagicMock()
        mgr = DefaultWorkerManager(task_repo=mock_task_repo, event_repo=mock_event_repo)
        rec = mgr.start_worker(WorkerMode.SINGLE_BROWSER)
        assert rec.status == WorkerStatus.IDLE
        assert mgr.active_count == 1
        assert mgr.stop_worker(rec.id) is True
        assert mgr.active_count == 0

    def test_shutdown_all(self):
        from backend.workers.default_manager import DefaultWorkerManager
        set_settings(AppSettings(max_workers=3))
        mock_task_repo = MagicMock()
        mock_event_repo = MagicMock()
        mgr = DefaultWorkerManager(task_repo=mock_task_repo, event_repo=mock_event_repo)
        mgr.start_worker(WorkerMode.SINGLE_BROWSER)
        mgr.start_worker(WorkerMode.SINGLE_BROWSER)
        assert mgr.active_count == 2
        mgr.shutdown_all()
        assert mgr.active_count == 0

    def test_list_workers(self):
        from backend.workers.default_manager import DefaultWorkerManager
        set_settings(AppSettings(max_workers=2))
        mock_task_repo = MagicMock()
        mock_event_repo = MagicMock()
        mgr = DefaultWorkerManager(task_repo=mock_task_repo, event_repo=mock_event_repo)
        mgr.start_worker(WorkerMode.SINGLE_BROWSER)
        workers = mgr.list_workers()
        assert len(workers) == 1


# ── Task Executor Claim Guard ─────────────────────────────────────────────

class TestTaskExecutorClaimGuard:
    """TaskExecutor must refuse to execute unclaimed tasks."""

    def test_rejects_task_not_in_running(self):
        from backend.automation.task_executor import TaskExecutor
        mock_task_repo = MagicMock()
        mock_event_repo = MagicMock()
        mock_error_repo = MagicMock()
        task = Task(
            id="t1", contact_id="c1", type=TaskType.MESSAGE,
            status=TaskState.READY,
        )
        # get_by_id returns READY task (not RUNNING)
        mock_task_repo.get_by_id.return_value = task
        executor = TaskExecutor(mock_task_repo, mock_event_repo, mock_error_repo)
        ctx = ExecutionContext(worker_id="w1")
        adapter = MagicMock()
        result = executor.execute_source_sync(task, ctx, adapter)
        assert result is False

    def test_rejects_task_without_lock(self):
        from backend.automation.task_executor import TaskExecutor
        mock_task_repo = MagicMock()
        mock_event_repo = MagicMock()
        mock_error_repo = MagicMock()
        task = Task(
            id="t2", contact_id="c2", type=TaskType.MESSAGE,
            status=TaskState.RUNNING, lock_token=None,
        )
        mock_task_repo.get_by_id.return_value = task
        executor = TaskExecutor(mock_task_repo, mock_event_repo, mock_error_repo)
        ctx = ExecutionContext(worker_id="w1")
        adapter = MagicMock()
        result = executor.execute_source_sync(task, ctx, adapter)
        assert result is False

    def test_rejects_wrong_worker(self):
        from backend.automation.task_executor import TaskExecutor
        mock_task_repo = MagicMock()
        mock_event_repo = MagicMock()
        mock_error_repo = MagicMock()
        task = Task(
            id="t3", contact_id="c3", type=TaskType.MESSAGE,
            status=TaskState.RUNNING, lock_token="LK1", worker_id="w-other",
        )
        mock_task_repo.get_by_id.return_value = task
        executor = TaskExecutor(mock_task_repo, mock_event_repo, mock_error_repo)
        ctx = ExecutionContext(worker_id="w1")
        adapter = MagicMock()
        result = executor.execute_source_sync(task, ctx, adapter)
        assert result is False


# ── Recovery / Reconciliation ──────────────────────────────────────────────

class TestRecoveryUnit:
    def _make_repos(self, tmp_path):
        from backend.database.manager import DatabaseManager
        from backend.database.migrations import MigrationRunner
        from backend.repositories.task_repo import TaskRepository
        from backend.repositories.event_repo import EventRepository
        from backend.repositories.error_repo import ErrorRepository
        from backend.repositories.contact_repo import ContactRepository
        from backend.domain.models import Contact
        db_path = str(tmp_path / "test_recovery.db")
        db = DatabaseManager(db_path)
        MigrationRunner(db).apply_pending()
        contact_repo = ContactRepository(db)
        for cid in ["c1", "c2", "c3"]:
            contact_repo.create(Contact(id=cid, name=f"Contact {cid}", instagram_url=f"https://instagram.com/{cid}"))
        return TaskRepository(db), EventRepository(db), ErrorRepository(db), db

    def test_mark_running_as_interrupted(self, tmp_path):
        task_repo, event_repo, error_repo, db = self._make_repos(tmp_path)
        task = Task(id="t-run", contact_id="c1", type=TaskType.MESSAGE, status=TaskState.READY)
        task_repo.create(task)
        task_repo.claim_task("t-run", "w1", "lock1")
        # Now task is RUNNING
        count = task_repo.mark_running_as_interrupted()
        assert count == 1
        t = task_repo.get_by_id("t-run")
        assert t.status == TaskState.INTERRUPTED
        assert t.lock_token is None  # lock cleared

    def test_recover_interrupted_requeues(self, tmp_path):
        from backend.automation.recovery import TaskReconciliationService
        task_repo, event_repo, error_repo, db = self._make_repos(tmp_path)
        task = Task(id="t-int", contact_id="c2", type=TaskType.MESSAGE, status=TaskState.READY)
        task_repo.create(task)
        task_repo.claim_task("t-int", "w1", "lock2")
        task_repo.mark_running_as_interrupted()
        recovery = TaskReconciliationService(task_repo, event_repo, error_repo)
        recovered = recovery.recover_interrupted_tasks()
        assert len(recovered) == 1
        assert recovered[0].status == TaskState.QUEUED

    def test_unknown_result_goes_to_manual_review(self, tmp_path):
        from backend.automation.recovery import TaskReconciliationService
        task_repo, event_repo, error_repo, db = self._make_repos(tmp_path)
        task = Task(id="t-unk", contact_id="c3", type=TaskType.MESSAGE, status=TaskState.READY)
        task_repo.create(task)
        task_repo.claim_task("t-unk", "w1", "lock3")
        task_repo.update_state("t-unk", TaskState.RECONCILING, enforce_transition=True)
        recovery = TaskReconciliationService(task_repo, event_repo, error_repo)
        result = recovery.reconcile_task("t-unk", verification_confirmed=None, details="cannot determine")
        assert result.status == TaskState.MANUAL_REVIEW
