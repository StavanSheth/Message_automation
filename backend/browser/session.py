"""Controlled browser session abstraction."""

import time
from typing import Optional, Dict, Any

from backend.domain.models import utc_now_iso
from backend.domain.enums import ErrorCode
from backend.browser.browser_types import (
    BrowserType,
    SessionStatus,
    BrowserLaunchConfig,
    BrowserHealthResult,
    BrowserSessionInfo,
)
from backend.browser.driver import BrowserDriver, PlaywrightBrowserDriver
from backend.browser.exceptions import (
    BrowserException,
    BrowserCrashError,
    BrowserTimeoutError,
    BrowserNavigationError,
    BrowserSessionError,
)
from backend.events.logger import get_logger

logger = get_logger("browser_session")


class BrowserSessionInstance:
    """
    Encapsulates a managed, state-tracked browser session.
    Protects sensitive credentials and cookies from logging.
    """
    auth_status: Optional[str] = None
    account_id: Optional[str] = None
    worker_id: Optional[str] = None

    def __init__(
        self,
        session_id: str,
        driver: Optional[BrowserDriver] = None,
        worker_id: Optional[str] = None,
        browser_type: BrowserType = BrowserType.CHROMIUM,
        profile_path: Optional[str] = None,
        profile_id: Optional[str] = None,
        config: Optional[BrowserLaunchConfig] = None,
        account_id: Optional[str] = None,
    ):
        self.session_id = session_id
        self.worker_id = worker_id
        self.account_id = account_id
        self.browser_type = browser_type
        self.profile_path = profile_path
        self.profile_id = profile_id
        self.config = config or BrowserLaunchConfig(
            browser_type=browser_type,
            profile_directory=profile_path,
        )
        self.driver: BrowserDriver = driver or PlaywrightBrowserDriver(self.config)
        self.status: SessionStatus = (
            SessionStatus.READY if (driver and driver.is_connected()) else SessionStatus.NOT_STARTED
        )
        self.auth_status: Optional[str] = None
        self.created_at: str = utc_now_iso()
        self.last_activity_at: str = self.created_at
        self.current_url: str = ""
        self.current_title: str = ""
        self.current_stage: str = "INITIALIZED"
        self.current_action: str = "IDLE"
        self.last_action: str = ""
        self.last_action_timestamp: str = self.created_at
        self.last_error: Optional[str] = None
        self.screenshot_path: Optional[str] = None

    def transition_to(self, target_status: SessionStatus, reason: Optional[str] = None) -> None:
        """Enforce strict session state transitions."""
        old_status = self.status
        self.status = target_status
        self.last_activity_at = utc_now_iso()
        if reason:
            self.last_error = reason
        logger.info(
            f"Session {self.session_id} state transition: {old_status.value} -> {target_status.value} ({reason or 'normal'})"
        )

    def handle_browser_crash(self, reason: str = "Browser process exited unexpectedly") -> None:
        self.transition_to(SessionStatus.RECOVERING, reason=reason)

    def handle_context_crash(self, reason: str = "Browser context destroyed") -> None:
        self.transition_to(SessionStatus.RECOVERING, reason=reason)

    def handle_page_crash(self, reason: str = "Active browser page crashed") -> None:
        self.transition_to(SessionStatus.RECOVERING, reason=reason)

    def handle_worker_crash(self, reason: str = "Assigned worker terminated") -> None:
        self.transition_to(SessionStatus.RECOVERING, reason=reason)

    def handle_network_failure(self, reason: str = "Network connectivity lost") -> None:
        self.last_error = reason
        self.transition_to(SessionStatus.RECOVERING, reason=reason)

    def handle_auth_failure(self, reason: str = "Authentication required or expired") -> None:
        self.auth_status = "AUTH_REQUIRED"
        self.transition_to(SessionStatus.AUTH_REQUIRED, reason=reason)

    def update_action(
        self,
        stage: str,
        action: str,
        error: Optional[str] = None,
        screenshot_path: Optional[str] = None,
        url: Optional[str] = None,
    ) -> None:
        """Update live observation model for dashboard/API inspection."""
        now_iso = utc_now_iso()
        self.last_action = self.current_action
        self.current_stage = stage
        self.current_action = action
        self.last_action_timestamp = now_iso
        self.last_activity_at = now_iso
        if url:
            self.current_url = url
        if error:
            self.last_error = error
        if screenshot_path:
            self.screenshot_path = screenshot_path
        if self.is_alive():
            try:
                if not url:
                    self.current_url = self.driver.current_url()
                self.current_title = getattr(self.driver, "title", lambda: "")()
            except Exception:
                pass

    def start(self) -> None:
        """Start the browser session, following NOT_STARTED -> STARTING -> OPEN -> READY lifecycle."""
        if self.status in (SessionStatus.READY, SessionStatus.BUSY, SessionStatus.ACTIVE):
            return

        self.transition_to(SessionStatus.STARTING)
        try:
            self.driver.launch(self.config)
            self.transition_to(SessionStatus.OPEN)

            if not self.driver.is_connected():
                raise BrowserCrashError("Driver failed to connect after launch")
            if not self.driver.verify_alive():
                raise BrowserCrashError("Browser page is not responsive after launch")

            self.transition_to(SessionStatus.READY)
            self.current_stage = "READY"
            self.current_action = "SESSION_READY"
            try:
                self.current_url = self.driver.current_url()
                self.current_title = getattr(self.driver, "title", lambda: "")()
            except Exception:
                pass
            self.last_activity_at = utc_now_iso()
            logger.info("Browser session started", session_id=self.session_id, worker_id=self.worker_id)
        except Exception as e:
            self.transition_to(SessionStatus.RECOVERING, reason=f"Session startup failed: {e}")
            self.transition_to(SessionStatus.CRASHED, reason=f"Session startup failed: {e}")
            logger.error(f"Failed to start browser session {self.session_id}: {e}")
            if isinstance(e, BrowserException):
                raise
            raise BrowserSessionError(f"Session startup failed: {e}", code=ErrorCode.BROWSER_CRASH) from e

    def stop(self) -> None:
        """Stop and tear down the browser session cleanly."""
        if self.status in (SessionStatus.STOPPED, SessionStatus.CLOSED):
            return

        self.transition_to(SessionStatus.STOPPING)
        try:
            self.driver.close()
        except Exception as e:
            logger.warning(f"Error while closing driver in session {self.session_id}: {e}")
        finally:
            self.transition_to(SessionStatus.CLOSED)
            self.status = SessionStatus.STOPPED
            self.last_activity_at = utc_now_iso()
            logger.info("Browser session stopped", session_id=self.session_id)

    def restart(self) -> None:
        """Restart session cleanly."""
        logger.info("Restarting browser session", session_id=self.session_id)
        self.stop()
        self.start()

    ACTIVE_STATES = (
        SessionStatus.READY,
        SessionStatus.RUNNING,
        SessionStatus.ACTIVE,
        SessionStatus.OPEN,
        SessionStatus.AUTHENTICATED,
        SessionStatus.BUSY,
        SessionStatus.IDLE,
    )

    def is_alive(self) -> bool:
        """Check if driver is currently connected and responsive."""
        if self.status not in self.ACTIVE_STATES:
            return False
        return self.driver.is_connected()

    def navigate(self, url: str, timeout_ms: Optional[int] = None) -> str:
        """Navigate to URL, tracking activity time and current URL."""
        if not self.is_alive():
            raise BrowserCrashError(f"Session {self.session_id} is not alive for navigation", code=ErrorCode.BROWSER_CRASH)

        self.status = SessionStatus.BUSY
        try:
            loaded_url = self.driver.navigate(url, timeout_ms=timeout_ms)
            self.current_url = loaded_url
            self.last_activity_at = utc_now_iso()
            self.status = SessionStatus.READY
            return loaded_url
        except Exception as e:
            if isinstance(e, BrowserCrashError):
                self.handle_browser_crash(str(e))
            elif isinstance(e, BrowserTimeoutError):
                self.handle_network_failure(str(e))
            else:
                self.status = SessionStatus.READY
            raise

    def get_current_url(self) -> str:
        if self.is_alive():
            try:
                self.current_url = self.driver.current_url()
            except Exception:
                pass
        return self.current_url

    def new_tab(self, url: Optional[str] = None) -> str:
        """Open a new tab in the active browser context and optionally navigate to URL."""
        if not self.is_alive():
            raise BrowserCrashError(f"Session {self.session_id} is not alive for new_tab", code=ErrorCode.BROWSER_CRASH)
        self.status = SessionStatus.BUSY
        try:
            self.driver.new_page()
            self.status = SessionStatus.READY
            if url:
                return self.navigate(url)
            return self.get_current_url()
        except Exception:
            self.status = SessionStatus.READY
            raise

    def close_tab(self) -> None:
        """Close the active tab in the browser context."""
        if self.is_alive():
            try:
                self.driver.close_page()
                self.current_url = self.get_current_url()
            except Exception as e:
                logger.warning(f"Error closing tab in session {self.session_id}: {e}")

    def evaluate(self, expression: str, arg: Any = None) -> Any:
        """Evaluate JavaScript safely in page."""
        if not self.is_alive():
            raise BrowserCrashError(f"Session {self.session_id} is not alive for evaluate")
        self.last_activity_at = utc_now_iso()
        return self.driver.evaluate(expression, arg)

    def health_check(self) -> BrowserHealthResult:
        """Perform responsive health check."""
        if self.status not in self.ACTIVE_STATES:
            return BrowserHealthResult(
                healthy=False,
                browser_connected=False,
                context_available=False,
                page_available=False,
                current_url=self.current_url,
                latency_ms=0.0,
                error_code=ErrorCode.SOURCE_UNAVAILABLE.value,
                details=f"Session is in {self.status.value} state",
            )

        start_time = time.perf_counter()
        connected = self.driver.is_connected()
        latency_ms = (time.perf_counter() - start_time) * 1000.0

        if not connected:
            self.status = SessionStatus.CRASHED
            return BrowserHealthResult(
                healthy=False,
                browser_connected=False,
                context_available=False,
                page_available=False,
                current_url=self.current_url,
                latency_ms=latency_ms,
                error_code=ErrorCode.BROWSER_CRASH.value,
                details="Browser driver is disconnected",
            )

        try:
            curr_url = self.driver.current_url()
            self.current_url = curr_url
            return BrowserHealthResult(
                healthy=True,
                browser_connected=True,
                context_available=True,
                page_available=True,
                current_url=curr_url,
                latency_ms=latency_ms,
            )
        except Exception as e:
            self.status = SessionStatus.CRASHED
            return BrowserHealthResult(
                healthy=False,
                browser_connected=True,
                context_available=False,
                page_available=False,
                current_url=self.current_url,
                latency_ms=latency_ms,
                error_code=ErrorCode.BROWSER_CRASH.value,
                details=str(e),
            )

    def to_info(self) -> BrowserSessionInfo:
        p_pid = getattr(self.driver, "pid", None)
        return BrowserSessionInfo(
            session_id=self.session_id,
            worker_id=self.worker_id,
            account_id=self.account_id,
            browser_type=self.browser_type,
            profile_id=self.profile_id,
            status=self.status,
            created_at=self.created_at,
            last_activity_at=self.last_activity_at,
            current_url=self.current_url,
            pid=p_pid,
            browser_pid=p_pid,
            profile_path=self.profile_path,
            current_title=self.current_title,
            current_stage=self.current_stage,
            current_action=self.current_action,
            last_action=self.last_action,
            last_action_timestamp=self.last_action_timestamp,
            auth_status=self.auth_status,
            health="HEALTHY" if self.is_alive() else self.status.value,
            last_error=self.last_error,
            screenshot_path=self.screenshot_path,
        )
