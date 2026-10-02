"""Production-grade Scheduler Engine coordinating dispatching, retries, and follow-ups."""

import threading
from typing import List, Optional, Any
from backend.domain.models import Task, Followup, utc_now_iso
from backend.domain.enums import TaskState, TaskType, FollowupStatus, RepliedStatus
from backend.domain.errors import DuplicateTaskError
from backend.repositories.task_repo import TaskRepository
from backend.repositories.followup_repo import FollowupRepository
from backend.repositories.contact_repo import ContactRepository
from backend.scheduler.task_dispatcher import TaskDispatcher
from backend.scheduler.retry_scheduler import RetryScheduler
from backend.repositories.message_repo import MessageRepository
from backend.events.correlation import generate_id
from backend.events.logger import get_logger
from backend.config.settings import get_settings

logger = get_logger("scheduler")


class Scheduler:
    """
    Central automation scheduler coordinating:
    - Thread-controlled background loop with clean shutdown.
    - Due follow-up evaluation and atomic task materialization via FollowupService.
    - Prerequisite task validation before scheduling follow-ups.
    - Automatic cancellation when contact replies.
    - Retry evaluation and backoff via RetryScheduler.
    - Worker capacity respecting dispatching via TaskDispatcher.
    - Crash/stale task recovery coordination.
    """

    def __init__(
        self,
        task_repo: TaskRepository,
        followup_repo: FollowupRepository,
        contact_repo: ContactRepository,
        worker_manager: Optional[Any] = None,
        recovery_service: Optional[Any] = None,
        poll_interval: float = 1.0,
        task_dispatcher: Optional[TaskDispatcher] = None,
        retry_scheduler: Optional[RetryScheduler] = None,
        followup_service: Optional[Any] = None,
        message_repo: Optional[MessageRepository] = None,
        control_service: Optional[Any] = None,
        throttling_service: Optional[Any] = None,
        retention_service: Optional[Any] = None,
        auth_validator: Optional[Any] = None,
    ):
        self.task_repo = task_repo
        self.followup_repo = followup_repo
        self.contact_repo = contact_repo
        self._worker_manager = worker_manager
        self.recovery_service = recovery_service
        self.poll_interval = poll_interval
        self.message_repo = message_repo or MessageRepository(task_repo.db)
        self.control_service = control_service
        self.throttling_service = throttling_service
        self.retention_service = retention_service
        self._last_retention_sweep_at: float = 0.0
        self.retention_interval_seconds: int = getattr(get_settings(), "retention_interval_seconds", 86400)

        if auth_validator is not None:
            self.auth_validator = auth_validator
        else:
            try:
                from backend.browser.instagram.auth_validator import InstagramAuthValidator
                self.auth_validator = InstagramAuthValidator()
            except Exception:
                self.auth_validator = None

        self.task_dispatcher = task_dispatcher or TaskDispatcher(
            task_repo=task_repo,
            worker_manager=worker_manager,
            throttling_service=throttling_service,
            control_service=control_service,
            auth_validator=self.auth_validator,
        )
        self.retry_scheduler = retry_scheduler or RetryScheduler(task_repo)
        if followup_service is not None:
            self.followup_service = followup_service
        else:
            from backend.application.followup_service import FollowupService
            self.followup_service = FollowupService(
                followup_repo=self.followup_repo,
                task_repo=self.task_repo,
                contact_repo=self.contact_repo,
                message_repo=self.message_repo,
            )

        self.is_paused: bool = False
        self.is_running: bool = False

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    @property
    def worker_manager(self) -> Optional[Any]:
        return self._worker_manager

    @worker_manager.setter
    def worker_manager(self, value: Optional[Any]) -> None:
        self._worker_manager = value
        if hasattr(self, "task_dispatcher") and self.task_dispatcher:
            self.task_dispatcher.worker_manager = value

    def start(self) -> None:
        """Start scheduler background loop thread."""
        with self._lock:
            if self.is_running:
                return
            self.is_running = True
            self.is_paused = False
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="scheduler-engine",
                daemon=True,
            )
            self._thread.start()
        logger.info("Scheduler service started", poll_interval=self.poll_interval)

    def stop(self) -> None:
        """Stop scheduler background loop cleanly."""
        with self._lock:
            if not self.is_running:
                return
            self.is_running = False
            self._stop_event.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        logger.info("Scheduler service stopped")

    def pause(self) -> None:
        """Pause scheduler loop."""
        with self._lock:
            self.is_paused = True
        logger.info("Scheduler service paused")

    def resume(self) -> None:
        """Resume scheduler loop."""
        with self._lock:
            self.is_paused = False
        logger.info("Scheduler service resumed")

    def _run_loop(self) -> None:
        """Controlled background polling loop with graceful shutdown."""
        while not self._stop_event.wait(timeout=self.poll_interval):
            if self.is_paused:
                continue
            try:
                self.tick()
            except Exception as e:
                logger.error(f"Error during scheduler tick: {e}", exc_info=True)

    def tick(self) -> List[Task]:
        """
        Single evaluation cycle:
        1. Honor paused state.
        2. Recover stale workers / interrupted tasks.
        3. Evaluate retries for tasks in RETRY_WAIT.
        4. Atomically materialize due follow-ups into Tasks.
        5. Query ready tasks.
        6. Dispatch ready tasks via TaskDispatcher.
        Returns list of ready tasks.
        """
        if self.is_paused:
            return []

        if self.control_service and hasattr(self.control_service, "state"):
            from backend.domain.enums import SystemState
            if self.control_service.state != SystemState.RUNNING:
                return []

        now_iso = utc_now_iso()

        # ── Step 0: Scheduled retention sweep (configurable interval) ──
        if self.retention_service:
            import time
            now_ts = time.time()
            if (now_ts - self._last_retention_sweep_at) >= self.retention_interval_seconds:
                try:
                    self.retention_service.cleanup_expired_data()
                    self._last_retention_sweep_at = now_ts
                except Exception as e:
                    logger.warning(f"Error during scheduled retention sweep: {e}")

        # ── Step 1: Recover stale workers and interrupted tasks ─────
        if self.worker_manager and hasattr(self.worker_manager, "recover_stale_workers"):
            try:
                self.worker_manager.recover_stale_workers()
            except Exception as e:
                logger.warning(f"Error recovering stale workers in tick: {e}")

        if self.recovery_service and hasattr(self.recovery_service, "recover_interrupted_tasks"):
            try:
                self.recovery_service.recover_interrupted_tasks()
            except Exception as e:
                logger.warning(f"Error recovering interrupted tasks in tick: {e}")

        # ── Step 2: Evaluate retries ───────────────────────────────
        try:
            self.retry_scheduler.evaluate_retries()
        except Exception as e:
            logger.warning(f"Error evaluating retries in tick: {e}")

        # ── Step 3: Materialize due follow-ups atomically via FollowupService ──
        try:
            self.followup_service.materialize_due_followups()
        except Exception as e:
            logger.warning(f"Error materializing follow-ups in tick: {e}")

        # ── Step 4: Query ready tasks ──────────────────────────────
        ready_tasks = self.task_repo.list_ready()

        # ── Step 4: Dispatch to workers exclusively via TaskDispatcher ───
        if self.task_dispatcher:
            try:
                self.task_dispatcher.dispatch_ready_tasks()
            except Exception as e:
                logger.warning(f"Error dispatching tasks via TaskDispatcher: {e}")

        return ready_tasks

    def schedule_followup(self, contact_id: str, sequence: int) -> bool:
        """Schedule a follow-up after prior message confirms."""
        fu = self.followup_repo.get_by_contact_and_sequence(contact_id, sequence)
        if not fu:
            return False
        return self.followup_repo.update_status(fu.id, FollowupStatus.SCHEDULED)

    def cancel_followups(self, contact_id: str, reason: str = "REPLIED") -> int:
        """Cancel pending follow-ups for a contact."""
        return self.followup_repo.cancel_pending_for_contact(contact_id, cancel_reason=reason)
