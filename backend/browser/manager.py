"""Browser Manager coordinating sessions, profiles, and hardware constraints."""

from typing import Any, Dict, List, Optional
from backend.browser.browser_types import (
    BrowserType,
    SessionStatus,
    BrowserLaunchConfig,
    BrowserHealthResult,
    BrowserSessionInfo,
)
from backend.browser.session import BrowserSessionInstance
from backend.browser.driver import BrowserDriver
from backend.browser.profiles import BrowserProfileManager
from backend.browser.lifecycle import BrowserLifecycleManager
from backend.browser.health import BrowserHealthChecker
from backend.browser.exceptions import BrowserSessionError, BrowserException
from backend.config.settings import AppSettings, get_settings
from backend.health import detect_hardware
from backend.domain.models import HardwareCapabilities, utc_now_iso
from backend.domain.enums import WorkerMode, ErrorCode, EventCode, EventLevel
from backend.repositories.event_repo import EventRepository
from backend.events.logger import get_logger
from backend.events.correlation import generate_id

logger = get_logger("browser_manager")


class BrowserManager:
    """
    Manages browser sessions, profiles, and concurrency limits based on hardware capabilities.
    """

    def __init__(
        self,
        settings: Optional[AppSettings] = None,
        event_repo: Optional[EventRepository] = None,
        profile_manager: Optional[BrowserProfileManager] = None,
        driver_factory: Optional[callable] = None,
        hardware_capabilities: Optional[HardwareCapabilities] = None,
    ):
        self.settings = settings or get_settings()
        self.event_repo = event_repo
        self.profile_manager = profile_manager or BrowserProfileManager(
            self.settings.browser_profile_directory
        )
        self.lifecycle_manager = BrowserLifecycleManager()
        self.driver_factory = driver_factory  # Optional custom factory for testing
        self.hardware: HardwareCapabilities = hardware_capabilities or detect_hardware()

        self._active_sessions: Dict[str, BrowserSessionInstance] = {}
        self._worker_session_map: Dict[str, str] = {}  # worker_id -> session_id

        # Determine effective worker mode and max concurrency
        self.effective_mode, self.effective_max_workers = self._resolve_mode_and_limits()
        logger.info(
            f"BrowserManager initialized in {self.effective_mode.value} mode with max {self.effective_max_workers} worker(s)",
            gpu_available=self.hardware.gpu_available,
            multi_available=self.hardware.multi_browser_available,
        )

    def _resolve_mode_and_limits(self) -> tuple[WorkerMode, int]:
        """
        Resolve SINGLE vs MULTI mode using hardware and settings.
        Handles AUTO / SINGLE / MULTI logic cleanly without crashing.
        """
        requested_mode_str = self.settings.worker_mode.upper()

        if requested_mode_str == "AUTO":
            if self.hardware.multi_browser_available:
                return WorkerMode.MULTI_BROWSER, min(self.settings.max_workers, self.hardware.recommended_max_workers)
            return WorkerMode.SINGLE_BROWSER, 1

        if requested_mode_str == WorkerMode.MULTI_BROWSER.value:
            if not self.hardware.multi_browser_available:
                logger.warning(
                    "MULTI_BROWSER mode requested but hardware does not meet requirements (NVIDIA GPU with >=2GB VRAM and >=4GB RAM). Falling back to SINGLE_BROWSER.",
                    requested_mode=requested_mode_str,
                    fallback_mode=WorkerMode.SINGLE_BROWSER.value,
                )
                if self.event_repo:
                    self.event_repo.record(
                        event_code=EventCode.WORKER_STARTED,
                        category="browser_manager",
                        level=EventLevel.WARNING,
                        entity_type="browser_manager",
                        entity_id=None,
                        payload={
                            "action": "mode_fallback",
                            "requested_mode": requested_mode_str,
                            "resolved_mode": WorkerMode.SINGLE_BROWSER.value,
                            "reason": "hardware_insufficient_for_multi_browser",
                        },
                    )
                return WorkerMode.SINGLE_BROWSER, 1
            return WorkerMode.MULTI_BROWSER, max(1, min(self.settings.max_workers, self.hardware.recommended_max_workers))

        return WorkerMode.SINGLE_BROWSER, 1

    def create_session(
        self,
        worker_id: Optional[str] = None,
        profile_name: Optional[str] = None,
        custom_driver: Optional[BrowserDriver] = None,
    ) -> BrowserSessionInstance:
        """
        Create and track a new browser session, enforcing concurrency and duplicate prevention.
        """
        # Prevent duplicate sessions for the same worker
        if worker_id and worker_id in self._worker_session_map:
            existing_sess_id = self._worker_session_map[worker_id]
            existing_sess = self._active_sessions.get(existing_sess_id)
            if existing_sess and existing_sess.is_alive():
                logger.info(
                    "Reusing existing active browser session for worker",
                    worker_id=worker_id,
                    session_id=existing_sess_id,
                )
                return existing_sess

        # Enforce maximum concurrent sessions
        active_count = len([s for s in self._active_sessions.values() if s.is_alive()])
        if active_count >= self.effective_max_workers:
            raise BrowserSessionError(
                f"Maximum concurrent browser sessions ({self.effective_max_workers}) reached for mode {self.effective_mode.value}",
                code=ErrorCode.SOURCE_UNAVAILABLE,
            )

        session_id = generate_id("BSESS")
        prof_name = profile_name or (f"worker_{worker_id}" if worker_id else f"session_{session_id}")
        profile = self.profile_manager.create_or_get_profile(prof_name)

        driver = custom_driver
        if not driver and self.driver_factory:
            driver = self.driver_factory()

        launch_config = BrowserLaunchConfig(
            browser_type=BrowserType(self.settings.browser_type.lower()),
            headless=self.settings.browser_headless,
            timeout_seconds=self.settings.browser_startup_timeout,
            profile_directory=profile.profile_path,
        )

        session = BrowserSessionInstance(
            session_id=session_id,
            driver=driver,
            worker_id=worker_id,
            browser_type=BrowserType(self.settings.browser_type.lower()),
            profile_path=profile.profile_path,
            profile_id=profile.profile_id,
            config=launch_config,
        )

        self._active_sessions[session_id] = session
        if worker_id:
            self._worker_session_map[worker_id] = session_id
        self.lifecycle_manager.register_session(session)

        if self.event_repo:
            self.event_repo.record(
                event_code=EventCode.BROWSER_SESSION_CREATED,
                category="browser",
                level=EventLevel.INFO,
                entity_type="browser_session",
                entity_id=session_id,
                payload={"worker_id": worker_id, "profile_id": profile.profile_id},
            )

        return session

    def get_session(self, session_id: str) -> Optional[BrowserSessionInstance]:
        return self._active_sessions.get(session_id)

    def get_session_for_worker(self, worker_id: str) -> Optional[BrowserSessionInstance]:
        sess_id = self._worker_session_map.get(worker_id)
        if sess_id:
            return self._active_sessions.get(sess_id)
        return None

    def stop_session(self, session_id: str) -> None:
        """Stop session and clean up mapping."""
        session = self._active_sessions.get(session_id)
        if session:
            session.stop()
            self.lifecycle_manager.stop_session(session_id)
            if session.worker_id and self._worker_session_map.get(session.worker_id) == session_id:
                del self._worker_session_map[session.worker_id]
            self._active_sessions.pop(session_id, None)

            if self.event_repo:
                self.event_repo.record(
                    event_code=EventCode.BROWSER_SESSION_CLOSED,
                    category="browser",
                    level=EventLevel.INFO,
                    entity_type="browser_session",
                    entity_id=session_id,
                    payload={"worker_id": session.worker_id},
                )

    def check_health(self) -> Dict[str, Any]:
        """Aggregate health status of all tracked sessions."""
        results = {}
        for sess_id, sess in self._active_sessions.items():
            results[sess_id] = BrowserHealthChecker.check_session(sess)
        return {
            "mode": self.effective_mode.value,
            "max_workers": self.effective_max_workers,
            "active_sessions": len(self._active_sessions),
            "sessions": {k: v.__dict__ for k, v in results.items()},
        }

    def shutdown(self) -> None:
        """Stop all sessions and tear down lifecycle manager."""
        logger.info("Shutting down BrowserManager and all active sessions")
        for sess_id in list(self._active_sessions.keys()):
            self.stop_session(sess_id)
        self.lifecycle_manager.cleanup_all()
