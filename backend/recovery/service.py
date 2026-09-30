"""Recovery service contract for interrupted tasks and crash recovery."""

from abc import ABC, abstractmethod
from typing import List
from backend.domain.models import Task


class RecoveryService(ABC):
    """Contract for recovering interrupted tasks and reconciling unknown send results."""

    @abstractmethod
    def reconcile_interrupted(self) -> List[Task]:
        """Inspect and reconcile tasks marked as INTERRUPTED."""
        pass

    @abstractmethod
    def reconcile_unknown_send(self, task_id: str) -> str:
        """Inspect Instagram conversation state to determine if an unknown-send succeeded."""
        pass

    @abstractmethod
    def recover_crashed_worker(self, worker_id: str) -> bool:
        """Handle crashed worker cleanup and task release."""
        pass
