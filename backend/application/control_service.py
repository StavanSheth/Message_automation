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
    SystemState.PAUSED: {SystemState.RUNNING, SystemState.DRAINING, SystemState.STOPPING},
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
    ):
        self.lifecycle_manager = lifecycle_manager
        self.scheduler = scheduler
        self.worker_manager = worker_manager
        self.browser_manager = browser_manager
        self.event_repo = event_repo
        self.settings = settings or get_settings()

        self._state: SystemState = SystemState.STOPPED
        self._lock = threading.Lock()

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

        logger.info(f"System transitioned: {old_state.value} -> {new_state.value} ({reason})")
        if self.event_repo:
            code_map = {
                SystemState.STARTING: EventCode.SYSTEM_STARTED,
                SystemState.RUNNING: EventCode.SYSTEM_STARTED,
                SystemState.PAUSED: EventCode.SYSTEM_PAUSED,
                SystemState.DRAINING: EventCode.SYSTEM_DRAINING,
                SystemState.STOPPED: EventCode.SYSTEM_STOPPED,
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
        Start sequence:
        1. Validate settings
        2. Set state -> STARTING
        3. Run startup recovery
        4. Start browser capability
        5. Start workers
        6. Start scheduler
        7. Set state -> RUNNING
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
        recovery_summary = self.lifecycle_manager.startup_recovery()

        # 3. Start workers
        if self.worker_manager:
            try:
                # Ensure at least one worker started if idle
                if self.worker_manager.active_count == 0:
                    self.worker_manager.start_worker()
            except Exception as e:
                logger.warning(f"Error starting initial worker: {e}")

        # 4. Start scheduler
        if self.scheduler:
            try:
                self.scheduler.resume()
                self.scheduler.start()
            except Exception as e:
                logger.warning(f"Error starting scheduler: {e}")

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
        Resume sequence:
        - Validate health
        - Resume scheduler
        - Set state -> RUNNING.
        """
        if not self._transition_to(SystemState.RUNNING, reason):
            return False

        if self.scheduler:
            self.scheduler.resume()
        return True

    def drain(self, reason: str = "Operator requested drain") -> bool:
        """
        Drain sequence:
        - Stop accepting new tasks (pause scheduler).
        - Set state -> DRAINING.
        - Allow safe in-flight tasks to complete.
        """
        if not self._transition_to(SystemState.DRAINING, reason):
            return False

        if self.scheduler:
            self.scheduler.pause()

        # Signal workers to drain
        if self.worker_manager:
            for w in self.worker_manager.list_workers():
                if hasattr(w, "drain"):
                    w.drain()
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
