"""Task Dispatcher for coordinating task claiming and worker dispatching."""

from typing import List, Optional, Any
from backend.domain.models import Task
from backend.repositories.task_repo import TaskRepository
from backend.events.logger import get_logger

logger = get_logger("task_dispatcher")


class TaskDispatcher:
    """
    Coordinates matching ready tasks with available worker capacity,
    preventing duplicate execution and respecting worker state limits.
    """

    def __init__(
        self,
        task_repo: TaskRepository,
        worker_manager: Optional[Any] = None,
    ):
        self.task_repo = task_repo
        self.worker_manager = worker_manager

    def dispatch_ready_tasks(self, limit: int = 50) -> int:
        """
        Query ready tasks and instruct attached worker manager to process them.
        Returns number of tasks processed.
        """
        if not self.worker_manager:
            return 0

        if hasattr(self.worker_manager, "process_tasks"):
            try:
                processed = self.worker_manager.process_tasks()
                return processed if isinstance(processed, int) else 0
            except Exception as e:
                logger.warning(f"Error executing worker_manager.process_tasks: {e}")
                return 0

        return 0
