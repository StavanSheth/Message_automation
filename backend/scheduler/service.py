"""Scheduler service contract and complete background scheduling engine."""

import threading
import time
from abc import ABC, abstractmethod
from typing import List, Optional, Any
from backend.domain.models import Task, Followup, utc_now_iso
from backend.domain.enums import TaskState, TaskType, FollowupStatus, RepliedStatus
from backend.domain.errors import DuplicateTaskError
from backend.repositories.task_repo import TaskRepository
from backend.repositories.followup_repo import FollowupRepository
from backend.repositories.contact_repo import ContactRepository
from backend.events.correlation import generate_id
from backend.events.logger import get_logger

logger = get_logger("scheduler_service")


class SchedulerService(ABC):
    """Contract for task scheduling and due follow-up processing."""

    @abstractmethod
    def start(self) -> None:
        """Start scheduler background loop."""
        pass

    @abstractmethod
    def pause(self) -> None:
        """Pause scheduler loop."""
        pass

    @abstractmethod
    def resume(self) -> None:
        """Resume scheduler loop."""
        pass

    @abstractmethod
    def stop(self) -> None:
        """Stop scheduler background loop cleanly."""
        pass

    @abstractmethod
    def tick(self) -> List[Task]:
        """Single scheduler evaluation step to dispatch due tasks."""
        pass

    @abstractmethod
    def schedule_followup(self, contact_id: str, sequence: int) -> bool:
        """Schedule a follow-up task when prerequisite completes."""
        pass

    @abstractmethod
    def cancel_followups(self, contact_id: str, reason: str = "REPLIED") -> int:
        """Cancel pending follow-ups for a contact."""
        pass


class SchedulerFoundationService(SchedulerService):
    """
    Production-grade scheduler engine implementing:
    - Thread-controlled background loop with clean shutdown.
    - Due follow-up evaluation and task instantiation.
    - Prerequisite task validation before scheduling follow-ups.
    - Automatic cancellation when contact replies.
    - Worker capacity respecting dispatching.
    - Crash/stale task recovery coordination.
    - Continuous operation after individual task failure.
    """

    def __init__(
        self,
        task_repo: TaskRepository,
        followup_repo: FollowupRepository,
        contact_repo: ContactRepository,
        worker_manager: Optional[Any] = None,
        recovery_service: Optional[Any] = None,
        poll_interval: float = 1.0,
    ):
        self.task_repo = task_repo
        self.followup_repo = followup_repo
        self.contact_repo = contact_repo
        self.worker_manager = worker_manager
        self.recovery_service = recovery_service
        self.poll_interval = poll_interval

        self.is_paused: bool = False
        self.is_running: bool = False

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

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
        2. Recover stale workers / interrupted tasks if recovery service attached.
        3. Evaluate due follow-ups (respecting replied status and prerequisite completion).
        4. Query ready tasks.
        5. Dispatch ready tasks to available workers if worker_manager attached.
        Returns list of ready tasks.
        """
        if self.is_paused:
            return []

        now_iso = utc_now_iso()

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

        # ── Step 2: Check due follow-ups ───────────────────────────
        due_followups = self.followup_repo.list_due(now_iso)
        for fu in due_followups:
            contact = self.contact_repo.get_by_id(fu.contact_id)
            if not contact or contact.replied_status == RepliedStatus.YES:
                self.followup_repo.update_status(fu.id, FollowupStatus.CANCELLED, cancel_reason="REPLIED")
                continue

            # Verify prerequisite completion before task instantiation if prerequisite exists
            if fu.sequence == 1:
                prereq = self.task_repo.get_by_contact_and_type(fu.contact_id, TaskType.MESSAGE, sequence=0)
                if prereq and prereq.status != TaskState.COMPLETED:
                    continue
            elif fu.sequence == 2:
                prereq = self.task_repo.get_by_contact_and_type(fu.contact_id, TaskType.FOLLOW_UP_1, sequence=1)
                if prereq and prereq.status != TaskState.COMPLETED:
                    continue

            task_type = TaskType.FOLLOW_UP_1 if fu.sequence == 1 else TaskType.FOLLOW_UP_2
            existing = self.task_repo.get_by_contact_and_type(fu.contact_id, task_type, sequence=fu.sequence)
            if not existing:
                try:
                    new_task = Task(
                        id=generate_id("TASK"),
                        contact_id=fu.contact_id,
                        type=task_type,
                        sequence=fu.sequence,
                        status=TaskState.READY,
                        scheduled_at=now_iso,
                        created_at=now_iso,
                        updated_at=now_iso,
                    )
                    self.task_repo.create(new_task)
                    self.followup_repo.update_status(fu.id, FollowupStatus.DUE)
                except DuplicateTaskError:
                    pass

        # ── Step 3: Query ready tasks ──────────────────────────────
        ready_tasks = self.task_repo.list_ready()

        # ── Step 4: Dispatch to workers if worker_manager attached ─
        if self.worker_manager and hasattr(self.worker_manager, "process_tasks"):
            try:
                self.worker_manager.process_tasks()
            except Exception as e:
                logger.warning(f"Error dispatching tasks to worker manager: {e}")

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
