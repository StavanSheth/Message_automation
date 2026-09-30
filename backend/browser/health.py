"""Browser health monitoring subsystem."""

import time
from typing import Optional

from backend.browser.browser_types import BrowserHealthResult
from backend.browser.session import BrowserSessionInstance
from backend.domain.enums import ErrorCode
from backend.events.logger import get_logger

logger = get_logger("browser_health")


class BrowserHealthChecker:
    """
    Performs verified health checks against browser sessions and drivers.
    Evaluates responsiveness, active connection, and execution latency.
    """

    @staticmethod
    def check_session(session: BrowserSessionInstance, timeout_seconds: float = 5.0) -> BrowserHealthResult:
        """Evaluate health of an active browser session with timeout enforcement."""
        start_time = time.perf_counter()

        if not session:
            return BrowserHealthResult(
                healthy=False,
                browser_connected=False,
                context_available=False,
                page_available=False,
                latency_ms=0.0,
                error_code=ErrorCode.SOURCE_UNAVAILABLE.value,
                details="No browser session provided",
            )

        try:
            result = session.health_check()
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            result.latency_ms = elapsed_ms

            if elapsed_ms > (timeout_seconds * 1000.0):
                result.healthy = False
                result.error_code = ErrorCode.TIMEOUT.value
                result.details = f"Health check exceeded timeout threshold ({elapsed_ms:.1f}ms > {timeout_seconds*1000}ms)"

            return result
        except Exception as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            logger.error(f"Health check failed with exception: {e}", session_id=session.session_id)
            return BrowserHealthResult(
                healthy=False,
                browser_connected=False,
                context_available=False,
                page_available=False,
                latency_ms=elapsed_ms,
                error_code=ErrorCode.BROWSER_CRASH.value,
                details=str(e),
            )
