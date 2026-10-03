"""Interactive Browser Startup Service ensuring reliable, visible Instagram session launch."""

import threading
from typing import Optional, Dict, Any

from backend.domain.enums import SessionAuthState, EventCode, EventLevel, WorkerStatus
from backend.domain.models import utc_now_iso
from backend.browser.session import BrowserSessionInstance
from backend.browser.instagram.auth_validator import InstagramAuthValidator
from backend.events.logger import get_logger

logger = get_logger("interactive_startup")


class InteractiveBrowserStartupService:
    """
    Coordinates idempotent visible browser startup for operators:
    1. Worker & Session creation (or reuse if already alive)
    2. Playwright initialization & visible Chrome/Chromium launch
    3. Navigation to Instagram
    4. Health verification
    5. Real Instagram authentication detection (LOGIN_REQUIRED, AUTHENTICATED, CHALLENGE, CHECKPOINT)
    6. Publishing worker/session state to event bus and audit repo
    """

    def __init__(
        self,
        worker_manager: Any,
        browser_manager: Any,
        auth_validator: Optional[InstagramAuthValidator] = None,
        event_repo: Optional[Any] = None,
        settings: Optional[Any] = None,
    ):
        self.worker_manager = worker_manager
        self.browser_manager = browser_manager
        self.auth_validator = auth_validator or InstagramAuthValidator()
        self.event_repo = event_repo
        self.settings = settings
        self._lock = threading.Lock()

    def launch_interactive_session(
        self,
        account_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        target_url: str = "https://www.instagram.com/",
        force_new: bool = False,
    ) -> Dict[str, Any]:
        """
        Idempotently launch or retrieve the operator's visible Instagram session.
        Calling repeatedly will not create runaway duplicate browser processes.
        """
        with self._lock:
            # 1. Idempotency check: see if a healthy worker & browser session already exists
            if not force_new and self.worker_manager:
                for w in self.worker_manager.list_workers():
                    if w.status not in (WorkerStatus.STOPPED, WorkerStatus.CRASHED):
                        # Check associated in-memory session
                        worker_obj = getattr(self.worker_manager, "_workers", {}).get(w.id)
                        sess = getattr(worker_obj, "session", None)
                        if sess and sess.is_alive():
                            logger.info(f"Reusing active interactive browser session for worker {w.id}")
                            return self._build_session_result(w.id, sess)

            # 2. Start a new worker with browser session
            if not self.worker_manager:
                raise RuntimeError("WorkerManager is required for interactive startup")

            worker_record = self.worker_manager.start_worker(
                worker_id=worker_id,
                account_id=account_id,
            )

            worker_obj = getattr(self.worker_manager, "_workers", {}).get(worker_record.id)
            if not worker_obj or not worker_obj.session:
                raise RuntimeError(f"Worker {worker_record.id} created without a browser session")

            session: BrowserSessionInstance = worker_obj.session

            # 3. Ensure session is started and alive
            if not session.is_alive():
                session.start()

            # 4. Navigate to Instagram target URL
            session.update_action(
                stage="NAVIGATING",
                action="Opening Instagram target",
                url=target_url,
            )
            try:
                session.navigate(target_url, timeout_ms=30000)
            except Exception as e:
                logger.warning(f"Initial navigation to {target_url} encountered an error: {e}")
                session.update_action(
                    stage="RECOVERING",
                    action=f"Navigation failed: {e}",
                    error=str(e),
                )

            # 5. Check browser health
            health_res = session.health_check()
            if not health_res.healthy:
                logger.error(f"Interactive browser health check failed: {health_res.details}")

            # 6. Check authentication state on the loaded page
            auth_state = SessionAuthState.UNKNOWN
            reason = "validator_unavailable"
            if self.auth_validator:
                auth_state, reason = self.auth_validator.check_auth_state(session)
                session.auth_status = auth_state.value

            # 7. Update session stage and action based on auth result
            if auth_state == SessionAuthState.AUTHENTICATED:
                session.update_action(stage="READY", action="Instagram authenticated; worker ready")
            elif auth_state == SessionAuthState.LOGIN_REQUIRED:
                session.update_action(stage="AUTH_REQUIRED", action="Instagram login required; awaiting manual login")
            elif auth_state in (SessionAuthState.CHALLENGE, SessionAuthState.CHECKPOINT):
                session.update_action(stage="CHALLENGE", action=f"Instagram checkpoint detected ({reason})")
            else:
                session.update_action(stage="STANDBY", action=f"Instagram opened ({reason})")

            # 8. Record event
            if self.event_repo:
                try:
                    self.event_repo.record(
                        event_code=EventCode.BROWSER_SESSION_CREATED,
                        category="browser",
                        level=EventLevel.INFO,
                        payload={
                            "worker_id": worker_record.id,
                            "session_id": session.session_id,
                            "auth_status": session.auth_status,
                            "url": session.current_url,
                            "pid": getattr(session.driver, "pid", None),
                        },
                    )
                except Exception:
                    pass

            return self._build_session_result(worker_record.id, session)

    def _build_session_result(self, worker_id: str, session: BrowserSessionInstance) -> Dict[str, Any]:
        driver_pid = getattr(session.driver, "pid", None)
        return {
            "status": "READY" if session.is_alive() else "FAILED",
            "worker_id": worker_id,
            "session_id": session.session_id,
            "browser_type": session.browser_type.value if hasattr(session.browser_type, "value") else str(session.browser_type),
            "headless": session.config.headless if session.config else False,
            "pid": driver_pid,
            "browser_pid": driver_pid,
            "current_url": session.current_url,
            "current_title": session.current_title,
            "auth_status": session.auth_status or "UNKNOWN",
            "healthy": session.is_alive(),
            "stage": session.current_stage,
            "action": session.current_action,
            "timestamp": utc_now_iso(),
        }
