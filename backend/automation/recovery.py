"""Reconciliation and crash recovery compatibility layer."""

from typing import Optional, List
from backend.domain.models import Task
from backend.repositories.task_repo import TaskRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.error_repo import ErrorRepository
from backend.recovery.service import DefaultRecoveryService


class TaskReconciliationService:
    """
    Compatibility wrapper delegating to authoritative DefaultRecoveryService.
    Preserves existing API:
    - enter_reconciliation(task_id, reason)
    - reconcile_task(task_id, verification_confirmed, details)
    - recover_interrupted_tasks()
    """

    def __init__(
        self,
        task_repo: TaskRepository,
        event_repo: EventRepository,
        error_repo: Optional[ErrorRepository] = None,
    ):
        self._service = DefaultRecoveryService(
            task_repo=task_repo,
            event_repo=event_repo,
            error_repo=error_repo,
        )

    def enter_reconciliation(self, task_id: str, reason: str = "") -> Task:
        return self._service.enter_reconciliation(task_id, reason)

    def reconcile_task(
        self,
        task_id: str,
        verification_confirmed: Optional[bool],
        details: Optional[str] = None,
    ) -> Task:
        return self._service.reconcile_task(task_id, verification_confirmed, details)

    def recover_interrupted_tasks(self, max_retries: int = 3) -> List[Task]:
        return self._service.recover_interrupted_tasks(max_retries=max_retries)
