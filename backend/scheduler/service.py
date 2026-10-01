"""Scheduler service interface contract and thin compatibility alias layer."""

from abc import ABC, abstractmethod
from typing import List

from backend.domain.models import Task
from backend.scheduler.scheduler import Scheduler


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


# Register authoritative Scheduler to satisfy SchedulerService interface contract
SchedulerService.register(Scheduler)

# Thin compatibility alias pointing directly to authoritative Scheduler engine
SchedulerFoundationService = Scheduler

__all__ = ["SchedulerService", "SchedulerFoundationService"]
