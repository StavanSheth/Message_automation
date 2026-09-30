"""Scheduler service interface contract."""

from abc import ABC, abstractmethod
from typing import List
from backend.domain.models import Task, Followup


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
    def tick(self) -> List[Task]:
        """Single scheduler evaluation step to dispatch due tasks."""
        pass

    @abstractmethod
    def schedule_followup(self, contact_id: str, sequence: int) -> bool:
        """Schedule a follow-up task when prerequisite completes."""
        pass

    @abstractmethod
    def cancel_followups(self, contact_id: str, reason: str) -> int:
        """Cancel pending follow-ups for a contact."""
        pass
