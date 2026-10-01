"""Task recovery handler applying safety policies to orphaned and interrupted tasks."""

from typing import List, Optional
from backend.domain.models import Task
from backend.domain.enums import TaskState, EventCode, EventLevel
from backend.recovery.policies import TaskRecoveryPolicy
from backend.repositories.task_repo import TaskRepository
from backend.repositories.event_repo import EventRepository
from backend.reconciliation.service import ReconciliationService
from backend.events.logger import get_logger

logger = get_logger("task_recovery")


class TaskRecoveryHandler:
    """Safely recovers tasks according to TaskRecoveryPolicy."""

    def __init__(
        self,
        task_repo: TaskRepository,
        reconciliation_service: Optional[ReconciliationService] = None,
        event_repo: Optional[EventRepository] = None,
    ):
        self.task_repo = task_repo
        self.reconciliation_service = reconciliation_service
        self.event_repo = event_repo

    def recover_task(self, task: Task, reason: str = "crash_recovery") -> TaskState:
        """
        Recover an individual task based on its state at time of interruption.
        """
        target = TaskRecoveryPolicy.get_recovery_target(task.status)

        if target == TaskState.RECONCILING:
            logger.warning(
                f"Task {task.id} in state {task.status.value} requires reconciliation; routing to ReconciliationService"
            )
            if self.reconciliation_service:
                self.reconciliation_service.enter_reconciliation(
                    task_id=task.id,
                    reason=f"{reason}_during_{task.status.value.lower()}",
                    worker_id=task.worker_id,
                )
            else:
                self.task_repo.update_state(task.id, TaskState.RECONCILING, enforce_transition=False)
        else:
            self.task_repo.update_state(task.id, target, enforce_transition=False)

        # Release lock or expired lease
        self.task_repo.unlock_task(task.id)

        if self.event_repo:
            self.event_repo.record(
                event_code=EventCode.TASK_RECOVERED,
                category="recovery",
                level=EventLevel.INFO,
                entity_type="task",
                entity_id=task.id,
                payload={"from_state": task.status.value, "target_state": target.value, "reason": reason},
            )

        return target

    def recover_expired_leases(self) -> int:
        """Scan and recover tasks whose lease has expired."""
        expired = self.task_repo.recover_expired_leases()
        recovered_count = 0
        for task in expired:
            self.recover_task(task, reason="lease_expired")
            recovered_count += 1
        return recovered_count
