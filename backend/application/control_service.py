"""Application control service managing operational states, pause/resume, draining, and lifecycle transitions."""

import threading
from typing import Optional, Dict, Any
from backend.domain.enums import SystemState, EventCode, EventLevel
from backend.repositories.event_repo import EventRepository
from backend.application.lifecycle import ApplicationLifecycleManager
from backend.scheduler.scheduler import Scheduler
from backend.workers.default_manager import DefaultWorkerManager
from backend.browser.manager import BrowserManager
from backend.config.settings import AppSettings, get_settings
from backend.events.logger import get_logger

logger = get_logger("control_service")

# Permitted state transitions according to the state machine (Section 3.2)
ALLOWED_STATE_TRANSITIONS = {
    SystemState.STOPPED: {SystemState.STARTING},
    SystemState.STARTING: {SystemState.RUNNING, SystemState.DEGRADED, SystemState.STOPPED},
    SystemState.RUNNING: {SystemState.PAUSED, SystemState.DRAINING, SystemState.DEGRADED, SystemState.STOPPING},
    SystemState.PAUSED: {SystemState.RUNNING, SystemState.DRAINING, SystemState.STOPPING, SystemState.DEGRADED, SystemState.MANUAL_INTERVENTION},
    SystemState.DRAINING: {SystemState.STOPPING, SystemState.STOPPED},
    SystemState.DEGRADED: {SystemState.RUNNING, SystemState.MANUAL_INTERVENTION, SystemState.STOPPING},
    SystemState.MANUAL_INTERVENTION: {SystemState.RUNNING, SystemState.STOPPING, SystemState.STOPPED},
    SystemState.STOPPING: {SystemState.STOPPED},
}


class ApplicationControlService:
    """
    Central operational control plane:
    - Governs top-level application states (STARTING, RUNNING, PAUSED, DRAINING, STOPPING, STOPPED, DEGRADED, MANUAL_INTERVENTION).
    - Coordinates start(), stop(), pause(), resume(), drain(), and status().
    - Disallows scheduler/worker task dispatch when system state prohibits execution.
    """

    def __init__(
        self,
        lifecycle_manager: ApplicationLifecycleManager,
        scheduler: Optional[Scheduler] = None,
        worker_manager: Optional[DefaultWorkerManager] = None,
        browser_manager: Optional[BrowserManager] = None,
        event_repo: Optional[EventRepository] = None,
        settings: Optional[AppSettings] = None,
        system_control_repo: Optional[Any] = None,
    ):
        self.lifecycle_manager = lifecycle_manager
        self.scheduler = scheduler
        self.worker_manager = worker_manager
        self.browser_manager = browser_manager
        self.event_repo = event_repo
        self.settings = settings or get_settings()

        # Persistent control repo
        if system_control_repo is not None:
            self.system_control_repo = system_control_repo
        elif hasattr(lifecycle_manager, "db"):
            from backend.repositories.system_control_repo import SystemControlRepository
            self.system_control_repo = SystemControlRepository(lifecycle_manager.db)
        else:
            self.system_control_repo = None

        self._lock = threading.Lock()

        # Recover persisted state on startup if available, rather than assuming STOPPED
        recovered_state = self.system_control_repo.get_state() if self.system_control_repo else None
        self._state: SystemState = recovered_state or SystemState.STOPPED

    @property
    def state(self) -> SystemState:
        with self._lock:
            return self._state

    def _transition_to(self, new_state: SystemState, reason: str = "") -> bool:
        with self._lock:
            allowed = ALLOWED_STATE_TRANSITIONS.get(self._state, set())
            if new_state not in allowed:
                logger.warning(
                    f"Illegal system state transition from {self._state.value} to {new_state.value} (reason: {reason})"
                )
                return False
            old_state = self._state
            self._state = new_state

        if self.system_control_repo:
            try:
                self.system_control_repo.set_state(new_state)
            except Exception as e:
                logger.warning(f"Failed to persist system state {new_state.value}: {e}")

        logger.info(f"System transitioned: {old_state.value} -> {new_state.value} ({reason})")
        if self.event_repo:
            code_map = {
                SystemState.STARTING: EventCode.SYSTEM_STARTED,
                SystemState.RUNNING: EventCode.SYSTEM_STARTED,
                SystemState.PAUSED: EventCode.SYSTEM_PAUSED,
                SystemState.DRAINING: EventCode.SYSTEM_DRAINING,
                SystemState.STOPPED: EventCode.SYSTEM_STOPPED,
                SystemState.DEGRADED: EventCode.TASK_STATE_CHANGED,
                SystemState.MANUAL_INTERVENTION: EventCode.TASK_STATE_CHANGED,
            }
            code = code_map.get(new_state, EventCode.TASK_STATE_CHANGED)
            self.event_repo.record(
                event_code=code,
                category="system",
                level=EventLevel.INFO if new_state in (SystemState.RUNNING, SystemState.STARTING) else EventLevel.WARNING,
                payload={"from_state": old_state.value, "to_state": new_state.value, "reason": reason},
            )
        return True

    def start(self) -> Dict[str, Any]:
        """
        Start sequence (Section 6):
        1. Set state -> STARTING
        2. Validate configuration
        3. Database startup recovery
        4. Start workers
        5. Start scheduler
        6. Validate component health
        7. If any mandatory component fails -> transition to DEGRADED or STOPPED, NEVER RUNNING.
        8. If healthy -> transition to RUNNING.
        """
        if not self._transition_to(SystemState.STARTING, "System startup initiated"):
            return {"status": "error", "message": f"Cannot start from state {self.state.value}"}

        # 1. Validate configuration
        try:
            if hasattr(self.settings, "validate_runtime_config"):
                self.settings.validate_runtime_config()
        except Exception as e:
            self._transition_to(SystemState.DEGRADED, f"Config validation failed: {e}")
            return {"status": "error", "message": f"Configuration invalid: {e}"}

        # 2. Run startup recovery
        try:
            recovery_summary = self.lifecycle_manager.startup_recovery()
        except Exception as e:
            logger.error(f"Startup recovery failed: {e}")
            self._transition_to(SystemState.DEGRADED, f"Startup recovery failed: {e}")
            return {"status": "error", "message": f"Recovery failed: {e}"}

        # 3. Start workers
        if self.worker_manager:
            try:
                if self.worker_manager.active_count == 0:
                    self.worker_manager.start_worker()
            except Exception as e:
                logger.error(f"Failed to start initial worker: {e}")
                self._transition_to(SystemState.DEGRADED, f"Worker startup failed: {e}")
                return {"status": "error", "message": f"Worker startup failed: {e}"}

        # 4. Start scheduler
        if self.scheduler:
            try:
                self.scheduler.resume()
                self.scheduler.start()
            except Exception as e:
                logger.error(f"Failed to start scheduler: {e}")
                self._transition_to(SystemState.DEGRADED, f"Scheduler startup failed: {e}")
                return {"status": "error", "message": f"Scheduler startup failed: {e}"}

        # 5. Component health validation
        if self.browser_manager and hasattr(self.browser_manager, "is_healthy") and not self.browser_manager.is_healthy():
            self._transition_to(SystemState.DEGRADED, "Browser manager reported unhealthy status")
            return {"status": "degraded", "message": "Browser manager unhealthy"}

        self._transition_to(SystemState.RUNNING, "Startup completed successfully")
        return {"status": "ok", "state": self.state.value, "recovery": recovery_summary}

    def pause(self, reason: str = "Operator requested pause") -> bool:
        """
        Pause sequence:
        - Stop scheduling new tasks (pause scheduler).
        - Allow currently running safe tasks to complete.
        - Set state -> PAUSED.
        """
        if not self._transition_to(SystemState.PAUSED, reason):
            return False

        if self.scheduler:
            self.scheduler.pause()
        return True

    def resume(self, reason: str = "Operator requested resume") -> bool:
        """
        Resume sequence (Section 7):
        - Validate system state
        - Validate browser health
        - Validate worker health and authentication
        - Check for challenge/checkpoint -> escalate to MANUAL_INTERVENTION if detected
        - Only resume scheduler after all mandatory checks pass
        """
        # Validate current state is resumable
        if self.state not in (SystemState.PAUSED, SystemState.DEGRADED):
            logger.warning(f"Cannot resume from state {self.state.value}")
            return False

        # 1. Browser health check
        if self.browser_manager and hasattr(self.browser_manager, "is_healthy"):
            if not self.browser_manager.is_healthy():
                logger.warning("Resume rejected: browser manager is unhealthy")
                self._transition_to(SystemState.DEGRADED, "Browser manager unhealthy during resume")
                return False

        # 2. Worker health & challenge/checkpoint inspection
        if self.worker_manager:
            if hasattr(self.worker_manager, "active_count") and self.worker_manager.active_count == 0:
                logger.warning("Resume rejected: no active workers available")
                self._transition_to(SystemState.DEGRADED, "No active workers available during resume")
                return False

            for worker_rec in self.worker_manager.list_workers():
                # If any worker was quarantined for challenge/checkpoint -> escalate to MANUAL_INTERVENTION
                reason_lower = str(getattr(worker_rec, "quarantine_reason", "") or "").lower()
                if "challenge" in reason_lower or "checkpoint" in reason_lower:
                    logger.warning("Resume escalated to MANUAL_INTERVENTION: challenge/checkpoint detected")
                    self._transition_to(SystemState.MANUAL_INTERVENTION, "Challenge or checkpoint pending resolution")
                    return False

            # Inspect active sessions for challenge/checkpoint/login
            if hasattr(self.worker_manager, "_workers"):
                for w in self.worker_manager._workers.values():
                    sess = getattr(w, "session", None)
                    if sess:
                        auth_st = getattr(sess, "auth_status", None)
                        if auth_st in ("CHALLENGE", "CHECKPOINT"):
                            logger.warning("Resume escalated to MANUAL_INTERVENTION: session checkpoint detected")
                            self._transition_to(SystemState.MANUAL_INTERVENTION, "Session checkpoint pending resolution")
                            return False
                        if auth_st in ("LOGIN_REQUIRED", "SESSION_EXPIRED"):
                            logger.warning("Resume degraded: session requires login")
                            self._transition_to(SystemState.DEGRADED, "Session requires login")
                            return False

        # 3. Transition to RUNNING
        if not self._transition_to(SystemState.RUNNING, reason):
            return False

        # 4. Resume scheduler only after state transition succeeds
        if self.scheduler:
            self.scheduler.resume()
        return True

    def drain(self, reason: str = "Operator requested drain") -> bool:
        """
        Drain sequence (Section 8):
        - Stop accepting new tasks (pause scheduler).
        - Set state -> DRAINING.
        - Drain all worker instances (allow in-flight safe tasks to finish).
        - Stop workers and transition system -> STOPPED.
        """
        if not self._transition_to(SystemState.DRAINING, reason):
            return False

        if self.scheduler:
            self.scheduler.pause()

        # Signal workers to drain
        if self.worker_manager:
            if hasattr(self.worker_manager, "drain_all"):
                self.worker_manager.drain_all(reason)
            else:
                for w in getattr(self.worker_manager, "_workers", {}).values():
                    if hasattr(w, "drain"):
                        w.drain(reason)

        return True

    def stop(self, reason: str = "Operator requested stop") -> None:
        """
        Stop sequence:
        - Transition STOPPING -> STOPPED.
        - Graceful shutdown of scheduler, workers, browsers, DB.
        """
        self._transition_to(SystemState.STOPPING, reason)
        self.lifecycle_manager.graceful_shutdown()
        self._transition_to(SystemState.STOPPED, "Shutdown complete")

    def status(self) -> Dict[str, Any]:
        """Return high-level operational status snapshot."""
        return {
            "system_state": self.state.value,
            "scheduler_paused": getattr(self.scheduler, "is_paused", False) if self.scheduler else True,
            "active_workers": self.worker_manager.active_count if self.worker_manager else 0,
            "effective_max_workers": getattr(self.worker_manager, "effective_max_workers", 0) if self.worker_manager else 0,
        }
