"""Task Dispatcher coordinating task claiming, eligibility checks, and worker dispatching."""

from typing import List, Optional, Any, Tuple
from backend.domain.models import Task
from backend.domain.enums import TaskState, SystemState
from backend.repositories.task_repo import TaskRepository
from backend.application.throttling_service import ThrottlingService
from backend.events.logger import get_logger

logger = get_logger("task_dispatcher")

INELIGIBLE_STATES = {
    TaskState.RECONCILING,
    TaskState.MANUAL_REVIEW,
    TaskState.CANCELLED,
    TaskState.COMPLETED,
    TaskState.FAILED,
    TaskState.SKIPPED,
    TaskState.RUNNING,
}


class TaskDispatcher:
    """
    Coordinates matching ready tasks with available worker capacity,
    enforcing:
    - System operational state
    - Task state eligibility (never dispatch RECONCILING, MANUAL_REVIEW, CANCELLED)
    - Throttling and cooldown checks
    - Worker availability and health
    """

    def __init__(
        self,
        task_repo: TaskRepository,
        worker_manager: Optional[Any] = None,
        throttling_service: Optional[ThrottlingService] = None,
        control_service: Optional[Any] = None,
    ):
        self.task_repo = task_repo
        self.worker_manager = worker_manager
        self.throttling_service = throttling_service
        self.control_service = control_service

    def is_task_eligible(self, task: Task) -> Tuple[bool, str]:
        """
        Evaluate whether a task is eligible for immediate worker dispatch.
        Never dispatch tasks in RECONCILING, MANUAL_REVIEW, CANCELLED, etc.
        """
        # 1. State check
        if task.status in INELIGIBLE_STATES:
            return False, f"Task {task.id} has ineligible status {task.status.value}"

        if task.status not in (TaskState.READY, TaskState.QUEUED):
            return False, f"Task {task.id} status is {task.status.value}, expected READY/QUEUED"

        # 2. System state check
        if self.control_service and hasattr(self.control_service, "state"):
            if self.control_service.state != SystemState.RUNNING:
                return False, f"System state is {self.control_service.state.value}, dispatch suspended"

        # 3. Throttling and cooldown checks
        if self.throttling_service:
            account_id = getattr(task, "account_id", None)
            allowed, reason = self.throttling_service.can_dispatch(account_id=account_id)
            if not allowed:
                return False, reason

        return True, "Eligible"

    def dispatch_ready_tasks(self, limit: int = 50) -> int:
        """
        Query ready tasks, verify eligibility, and dispatch to available workers.
        Returns number of tasks processed.
        """
        # Guard: check top-level system state
        if self.control_service and hasattr(self.control_service, "state"):
            if self.control_service.state != SystemState.RUNNING:
                logger.debug(f"Task dispatch skipped: system state is {self.control_service.state.value}")
                return 0

        # Guard: check global cooldown
        if self.throttling_service:
            allowed, reason = self.throttling_service.can_dispatch()
            if not allowed:
                logger.debug(f"Task dispatch blocked by throttling: {reason}")
                return 0

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
