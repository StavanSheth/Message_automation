"""Worker Manager interface contract."""

from abc import ABC, abstractmethod
from typing import List, Optional
from backend.domain.models import WorkerRecord
from backend.domain.enums import WorkerStatus, WorkerMode


class WorkerManager(ABC):
    """Contract for browser worker lifecycle and concurrency management."""

    @abstractmethod
    def start_worker(self, mode: WorkerMode) -> WorkerRecord:
        """Launch a browser worker."""
        pass

    @abstractmethod
    def stop_worker(self, worker_id: str) -> bool:
        """Stop a specific worker."""
        pass

    @abstractmethod
    def list_workers(self) -> List[WorkerRecord]:
        """List active and managed workers."""
        pass

    @abstractmethod
    def get_worker_status(self, worker_id: str) -> Optional[WorkerStatus]:
        """Get status of a specific worker."""
        pass
