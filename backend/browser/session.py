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
        self.status: SessionStatus = SessionStatus.NOT_STARTED
        self.auth_status: Optional[str] = None
        self.created_at: str = utc_now_iso()
        self.last_activity_at: str = self.created_at
        self.current_url: str = ""

    def start(self) -> None:
        """Start the browser session."""
        if self.status in (SessionStatus.READY, SessionStatus.BUSY):
            return

        self.status = SessionStatus.STARTING
        try:
            self.driver.launch(self.config)
            self.status = SessionStatus.READY
            self.last_activity_at = utc_now_iso()
            logger.info("Browser session started", session_id=self.session_id, worker_id=self.worker_id)
        except Exception as e:
            self.status = SessionStatus.CRASHED
            logger.error(f"Failed to start browser session: {e}", session_id=self.session_id)
            if isinstance(e, BrowserException):
                raise
            raise BrowserSessionError(f"Session startup failed: {e}", code=ErrorCode.BROWSER_CRASH) from e

    def stop(self) -> None:
        """Stop and tear down the browser session."""
        if self.status == SessionStatus.STOPPED:
            return

        self.status = SessionStatus.STOPPING
        try:
            self.driver.close()
        except Exception as e:
            logger.warning(f"Error while closing driver in session {self.session_id}: {e}")
        finally:
            self.status = SessionStatus.STOPPED
            self.last_activity_at = utc_now_iso()
            logger.info("Browser session stopped", session_id=self.session_id)

    def restart(self) -> None:
        """Restart session cleanly."""
        logger.info("Restarting browser session", session_id=self.session_id)
        self.stop()
        self.start()

    def is_alive(self) -> bool:
        """Check if driver is currently connected and responsive."""
        if self.status not in (SessionStatus.READY, SessionStatus.BUSY):
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
            self.status = SessionStatus.CRASHED if isinstance(e, BrowserCrashError) else SessionStatus.READY
            raise

    def get_current_url(self) -> str:
        if self.is_alive():
            try:
                self.current_url = self.driver.current_url()
            except Exception:
                pass
        return self.current_url

    def evaluate(self, expression: str, arg: Any = None) -> Any:
        """Evaluate JavaScript safely in page."""
        if not self.is_alive():
            raise BrowserCrashError(f"Session {self.session_id} is not alive for evaluate")
        self.last_activity_at = utc_now_iso()
        return self.driver.evaluate(expression, arg)

    def health_check(self) -> BrowserHealthResult:
        """Perform responsive health check."""
        if self.status not in (SessionStatus.READY, SessionStatus.BUSY):
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
        return BrowserSessionInfo(
            session_id=self.session_id,
            worker_id=self.worker_id,
            browser_type=self.browser_type,
            profile_id=self.profile_id,
            status=self.status,
            created_at=self.created_at,
            last_activity_at=self.last_activity_at,
            current_url=self.current_url,
        )
