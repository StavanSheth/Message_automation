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


from backend.scheduler.scheduler import Scheduler
from backend.scheduler.task_dispatcher import TaskDispatcher
from backend.scheduler.retry_scheduler import RetryScheduler


class SchedulerFoundationService(Scheduler, SchedulerService):
    """
    Production-grade scheduler engine implementing:
    - Thread-controlled background loop with clean shutdown.
    - Due follow-up evaluation and atomic task instantiation via FollowupService.
    - Prerequisite task validation before scheduling follow-ups.
    - Automatic cancellation when contact replies.
    - Retry evaluation via RetryScheduler with exponential backoff and jitter.
    - Worker capacity respecting dispatching via TaskDispatcher.
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
        task_dispatcher: Optional[Any] = None,
        retry_scheduler: Optional[Any] = None,
        followup_service: Optional[Any] = None,
        message_repo: Optional[Any] = None,
    ):
        super().__init__(
            task_repo=task_repo,
            followup_repo=followup_repo,
            contact_repo=contact_repo,
            worker_manager=worker_manager,
            recovery_service=recovery_service,
            poll_interval=poll_interval,
            task_dispatcher=task_dispatcher,
            retry_scheduler=retry_scheduler,
            followup_service=followup_service,
            message_repo=message_repo,
        )

