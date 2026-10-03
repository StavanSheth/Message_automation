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
        session_repo: Optional[Any] = None,
    ):
        self.settings = settings or get_settings()
        self.event_repo = event_repo
        self.session_repo = session_repo
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
            f"BrowserManager initialized in {self.effective_mode.value} mode with max {self.effective_max_workers} worker(s)"
        )

    def _resolve_mode_and_limits(self) -> tuple[WorkerMode, int]:
        """
        Resolve SINGLE_BROWSER vs MULTI_BROWSER mode using hardware and settings.
        Follows Section 11 of Product Functional Spec:
        - If MULTI_BROWSER requested, requires qualifying GPU (>=2GB VRAM) and RAM (>=4GB).
        - Falls back to SINGLE_BROWSER if requirements are not met.
        - SINGLE_BROWSER is always bounded to max 1 worker.
        """
        requested_mode_str = str(self.settings.worker_mode).upper()

        if requested_mode_str in (WorkerMode.MULTI_BROWSER.value, WorkerMode.MULTI_BROWSER.name):
            ram_ok = (self.hardware.total_ram_gb >= 4.0 or self.hardware.available_ram_gb >= 3.0)
            if not self.hardware.multi_browser_available or not ram_ok:
                reason = "insufficient_ram" if not ram_ok else "insufficient_gpu"
                logger.warning(
                    f"MULTI_BROWSER mode requested ({requested_mode_str}) but hardware does not meet requirements ({reason}). Falling back to SINGLE_BROWSER."
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
                            "reason": f"hardware_{reason}_for_multi_browser",
                        },
                    )
                return WorkerMode.SINGLE_BROWSER, 1

            effective_limit = max(1, min(self.settings.max_workers, self.hardware.recommended_max_workers))
            return WorkerMode.MULTI_BROWSER, effective_limit

        return WorkerMode.SINGLE_BROWSER, 1

    def create_session(
        self,
        worker_id: Optional[str] = None,
        profile_name: Optional[str] = None,
        custom_driver: Optional[BrowserDriver] = None,
        account_id: Optional[str] = None,
    ) -> BrowserSessionInstance:
        """
        Create and track a new browser session, enforcing concurrency, profile isolation,
        and duplicate prevention.
        """
        # Prevent duplicate sessions for the same worker
        if worker_id and worker_id in self._worker_session_map:
            existing_sess_id = self._worker_session_map[worker_id]
            existing_sess = self._active_sessions.get(existing_sess_id)
            if existing_sess and existing_sess.is_alive():
                logger.info(
                    f"Reusing existing active browser session for worker {worker_id}: {existing_sess_id}"
                )
                return existing_sess
            elif existing_sess_id:
                # Existing session is dead or missing, clean up stale mapping
                self.stop_session(existing_sess_id)

        # Clean up any dead/stopped sessions before checking capacity
        dead_session_ids = [
            sid for sid, sess in list(self._active_sessions.items())
            if not sess.is_alive() and sess.status in (SessionStatus.STOPPED, SessionStatus.CRASHED)
        ]
        for sid in dead_session_ids:
            self.stop_session(sid)

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

        # Profile isolation: verify profile is not in use by another active worker
        for s in self._active_sessions.values():
            if s.is_alive() and s.profile_id == profile.profile_id and s.worker_id != worker_id:
                raise BrowserSessionError(
                    f"Profile '{profile.profile_id}' is already in use by active worker '{s.worker_id}'. "
                    "Browser profiles cannot be shared between isolated workers.",
                    code=ErrorCode.SOURCE_UNAVAILABLE,
                )

        driver = custom_driver
        if not driver and self.driver_factory:
            try:
                driver = self.driver_factory()
            except Exception as e:
                raise BrowserSessionError(
                    f"Driver initialization failed: {e}",
                    code=ErrorCode.BROWSER_CRASH,
                ) from e

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
            account_id=account_id,
        )

        self._active_sessions[session_id] = session
        if worker_id:
            self._worker_session_map[worker_id] = session_id
        self.lifecycle_manager.register_session(session)

        if self.session_repo:
            try:
                from backend.domain.models import BrowserSession
                self.session_repo.create(
                    BrowserSession(
                        id=session_id,
                        profile_path=profile.profile_path,
                        status="STARTING",
                        worker_id=worker_id,
                        account_id=account_id,
                    )
                )
            except Exception as e:
                logger.warning(f"Failed to persist browser session {session_id} to database: {e}")

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

    def start_session(self, session_id: str) -> BrowserSessionInstance:
        """Start a created session, ensuring failed startups do not leave phantom sessions."""
        session = self._active_sessions.get(session_id)
        if not session:
            raise BrowserSessionError(f"Session {session_id} not found", code=ErrorCode.SOURCE_UNAVAILABLE)
        try:
            session.start()
            return session
        except Exception:
            self.stop_session(session_id)
            raise

    def get_session(self, session_id: str) -> Optional[BrowserSessionInstance]:
        return self._active_sessions.get(session_id)

    def get_session_for_worker(self, worker_id: str) -> Optional[BrowserSessionInstance]:
        sess_id = self._worker_session_map.get(worker_id)
        if sess_id:
            sess = self._active_sessions.get(sess_id)
            if sess and sess.is_alive():
                return sess
            # Clean up stale mapping if session is dead or missing
            self._worker_session_map.pop(worker_id, None)
        return None

    def stop_session(self, session_id: str) -> None:
        """Stop session, unregister lifecycle, and clean up worker mappings. Idempotent."""
        session = self._active_sessions.pop(session_id, None)
        if session:
            try:
                session.stop()
            except Exception as e:
                logger.warning(f"Error while stopping session {session_id}: {e}")

            try:
                self.lifecycle_manager.stop_session(session_id)
            except Exception as e:
                logger.warning(f"Error in lifecycle manager stopping {session_id}: {e}")

            if self.session_repo:
                try:
                    self.session_repo.close_session(session_id)
                except Exception as e:
                    logger.warning(f"Failed to record closed session in repo: {e}")

            if session.worker_id and self._worker_session_map.get(session.worker_id) == session_id:
                self._worker_session_map.pop(session.worker_id, None)

            if self.event_repo:
                try:
                    self.event_repo.record(
                        event_code=EventCode.BROWSER_SESSION_CLOSED,
                        category="browser",
                        level=EventLevel.INFO,
                        entity_type="browser_session",
                        entity_id=session_id,
                        payload={"worker_id": session.worker_id},
                    )
                except Exception:
                    pass
        else:
            # Also clean up any lingering worker mappings to this session_id
            for wid, sid in list(self._worker_session_map.items()):
                if sid == session_id:
                    self._worker_session_map.pop(wid, None)

    def check_health(self) -> Dict[str, Any]:
        """Aggregate health status of all tracked sessions."""
        results = {}
        for sess_id, sess in list(self._active_sessions.items()):
            results[sess_id] = BrowserHealthChecker.check_session(sess)
        return {
            "mode": self.effective_mode.value,
            "max_workers": self.effective_max_workers,
            "active_sessions": len([s for s in self._active_sessions.values() if s.is_alive()]),
            "sessions": {k: v.__dict__ for k, v in results.items()},
        }

    def is_healthy(self) -> bool:
        """Check if all currently tracked active browser sessions are responsive and alive."""
        dead_sessions = [sid for sid, sess in self._active_sessions.items() if not sess.is_alive()]
        for sid in dead_sessions:
            self.stop_session(sid)
        return True

    def recover_session(
        self,
        worker_id: str,
        account_id: Optional[str] = None,
        profile_name: Optional[str] = None,
    ) -> BrowserSessionInstance:
        """
        Self-healing session recovery:
        1. Stop broken / crashed session.
        2. Create replacement session for worker.
        3. Validate account ownership and return new session.
        """
        if worker_id in self._worker_session_map:
            old_sess_id = self._worker_session_map[worker_id]
            self.stop_session(old_sess_id)
        sess = self.create_session(
            worker_id=worker_id,
            account_id=account_id,
            profile_name=profile_name,
        )
        if not sess.is_alive():
            sess.start()
        return sess

    def close_all(self) -> None:
        """Alias for shutdown to cleanly close all sessions."""
        self.shutdown()

    def shutdown(self) -> None:
        """Stop all sessions and tear down lifecycle manager. Idempotent."""
        logger.info("Shutting down BrowserManager and all active sessions")
        for sess_id in list(self._active_sessions.keys()):
            self.stop_session(sess_id)
        self.lifecycle_manager.cleanup_all()
        self._active_sessions.clear()
        self._worker_session_map.clear()
