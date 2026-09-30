"""Reconciliation and crash recovery logic for tasks and browser operations."""

from typing import Optional, List
from backend.domain.models import Task, utc_now_iso
from backend.domain.enums import TaskState, ErrorCode, EventCode, EventLevel
from backend.repositories.task_repo import TaskRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.error_repo import ErrorRepository
from backend.events.logger import get_logger

logger = get_logger("automation_recovery")


class TaskReconciliationService:
    """
    Handles reconciliation for tasks that ended in an unknown or interrupted state.
    Enforces the invariant: UNKNOWN_RESULT -> RECONCILING -> verify state -> COMPLETED / FAILED / MANUAL_REVIEW.
    """

    def __init__(
        self,
        task_repo: TaskRepository,
        event_repo: EventRepository,
        error_repo: ErrorRepository,
    ):
        self.task_repo = task_repo
        self.event_repo = event_repo
        self.error_repo = error_repo

    def enter_reconciliation(self, task_id: str, reason: str) -> Task:
        """Move a task from RUNNING or FAILED into RECONCILING."""
        task = self.task_repo.update_state(task_id, TaskState.RECONCILING, enforce_transition=True)
        self.event_repo.record(
            event_code=EventCode.TASK_STATE_CHANGED,
            category="recovery",
            level=EventLevel.WARNING,
            entity_type="task",
            entity_id=task_id,
            payload={"action": "enter_reconciliation", "reason": reason},
        )
        logger.warning(f"Task {task_id} entered RECONCILING: {reason}")
        return task

    def reconcile_task(
        self,
        task_id: str,
        verification_confirmed: Optional[bool],
        details: Optional[str] = None,
    ) -> Task:
        """
        Complete reconciliation:
        - If verification_confirmed is True -> COMPLETED
        - If verification_confirmed is False -> FAILED
        - If verification_confirmed is None (indeterminate) -> MANUAL_REVIEW
        """
        if verification_confirmed is True:
            target_state = TaskState.COMPLETED
        elif verification_confirmed is False:
            target_state = TaskState.FAILED
        else:
            target_state = TaskState.MANUAL_REVIEW

        task = self.task_repo.update_state(task_id, target_state, enforce_transition=True)

        self.event_repo.record(
            event_code=EventCode.TASK_RECONCILED,
            category="recovery",
            level=EventLevel.INFO if target_state == TaskState.COMPLETED else EventLevel.WARNING,
            entity_type="task",
            entity_id=task_id,
            payload={
                "resolved_state": target_state.value,
                "verification_confirmed": verification_confirmed,
                "details": details,
            },
        )
        logger.info(f"Task {task_id} reconciled to {target_state.value} (details: {details})")
        return task

    def recover_interrupted_tasks(self) -> List[Task]:
        """
        Find tasks left INTERRUPTED (e.g. from crash or stale worker)
        and transition them safely to QUEUED or MANUAL_REVIEW depending on attempt count.
        """
        interrupted = self.task_repo.list_interrupted()
        recovered: List[Task] = []

        for task in interrupted:
            # If attempt_count is 0 or low, allow re-queuing
            if task.attempt_count < 3:
                updated = self.task_repo.update_state(task.id, TaskState.QUEUED, enforce_transition=False)
                recovered.append(updated)
                self.event_repo.record(
                    event_code=EventCode.TASK_RETRY_SCHEDULED,
                    category="recovery",
                    level=EventLevel.INFO,
                    entity_type="task",
                    entity_id=task.id,
                    payload={"action": "requeued_after_interruption", "attempts": task.attempt_count},
                )
            else:
                updated = self.task_repo.update_state(task.id, TaskState.MANUAL_REVIEW, enforce_transition=False)
                recovered.append(updated)
                self.event_repo.record(
                    event_code=EventCode.TASK_STATE_CHANGED,
                    category="recovery",
                    level=EventLevel.WARNING,
                    entity_type="task",
                    entity_id=task.id,
                    payload={"action": "routed_to_manual_review", "reason": "excessive_interruptions"},
                )

        return recovered
