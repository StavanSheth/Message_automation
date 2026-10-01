"""Authoritative recovery service and reconciliation engine for interrupted tasks and crashed workers."""

from abc import ABC, abstractmethod
from typing import List, Optional
from backend.domain.models import Task, utc_now_iso
from backend.domain.enums import TaskState, EventCode, EventLevel, ErrorCode
from backend.repositories.task_repo import TaskRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.error_repo import ErrorRepository
from backend.events.logger import get_logger

logger = get_logger("recovery_service")


class RecoveryService(ABC):
    """Contract for recovering interrupted tasks and reconciling unknown execution results."""

    @abstractmethod
    def reconcile_interrupted(self, action: str = "RECONCILING") -> List[Task]:
        """Inspect and reconcile tasks marked as INTERRUPTED."""
        pass

    @abstractmethod
    def enter_reconciliation(self, task_id: str, reason: str) -> Task:
        """Transition task into RECONCILING state."""
        pass

    @abstractmethod
    def reconcile_task(
        self,
        task_id: str,
        verification_confirmed: Optional[bool],
        details: Optional[str] = None,
    ) -> Task:
        """Resolve a reconciling task based on verification evidence."""
        pass

    @abstractmethod
    def recover_interrupted_tasks(self, max_retries: int = 3) -> List[Task]:
        """Re-queue interrupted tasks that have remaining retries or escalate to manual review."""
        pass

    @abstractmethod
    def reconcile_unknown_send(self, task_id: str, verification_confirmed: Optional[bool] = None) -> str:
        """Reconcile unknown message execution."""
        pass

    @abstractmethod
    def recover_crashed_worker(self, worker_id: str) -> int:
        """Handle crashed worker cleanup and task release."""
        pass


class DefaultRecoveryService(RecoveryService):
    """
    Authoritative recovery and reconciliation service implementing:
    RUNNING -> crash -> INTERRUPTED -> RECONCILING -> COMPLETED / FAILED / MANUAL_REVIEW
    and:
    INTERRUPTED -> safe retry -> QUEUED / READY.
    """

    def __init__(
        self,
        task_repo: TaskRepository,
        event_repo: Optional[EventRepository] = None,
        error_repo: Optional[ErrorRepository] = None,
    ):
        self.task_repo = task_repo
        self.event_repo = event_repo
        self.error_repo = error_repo

    def enter_reconciliation(self, task_id: str, reason: str = "") -> Task:
        """Move a task into RECONCILING state."""
        task = self.task_repo.update_state(task_id, TaskState.RECONCILING, enforce_transition=True)
        if self.event_repo:
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
        Complete reconciliation for a task:
        - verification_confirmed is True -> COMPLETED
        - verification_confirmed is False -> FAILED
        - verification_confirmed is None (indeterminate) -> MANUAL_REVIEW
        """
        if verification_confirmed is True:
            target_state = TaskState.COMPLETED
        elif verification_confirmed is False:
            target_state = TaskState.FAILED
        else:
            target_state = TaskState.MANUAL_REVIEW

        task = self.task_repo.update_state(task_id, target_state, enforce_transition=True)

        if self.event_repo:
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

    def recover_interrupted_tasks(self, max_retries: int = 3) -> List[Task]:
        """
        Recover tasks left in INTERRUPTED state:
        - If attempt_count < max_retries: transition to QUEUED for retry.
        - Else: transition to MANUAL_REVIEW.
        """
        interrupted = self.task_repo.list_interrupted()
        recovered: List[Task] = []

        for task in interrupted:
            if task.attempt_count < max_retries:
                updated = self.task_repo.update_state(task.id, TaskState.QUEUED, enforce_transition=False)
                recovered.append(updated)
                if self.event_repo:
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
                if self.event_repo:
                    self.event_repo.record(
                        event_code=EventCode.TASK_STATE_CHANGED,
                        category="recovery",
                        level=EventLevel.WARNING,
                        entity_type="task",
                        entity_id=task.id,
                        payload={"action": "escalated_to_manual_review_max_attempts", "attempts": task.attempt_count},
                    )

        return recovered

    def reconcile_interrupted(self, action: str = "RECONCILING") -> List[Task]:
        """
        Transition INTERRUPTED tasks to target state ('RECONCILING', 'READY', 'MANUAL_REVIEW').
        """
        target_state_map = {
            "RECONCILING": TaskState.RECONCILING,
            "READY": TaskState.READY,
            "MANUAL_REVIEW": TaskState.MANUAL_REVIEW,
        }
        target_state = target_state_map.get(action.upper(), TaskState.RECONCILING)

        interrupted = self.task_repo.list_interrupted()
        reconciled = []

        for task in interrupted:
            updated = self.task_repo.update_state(
                task_id=task.id,
                new_state=target_state,
                enforce_transition=True,
            )
            reconciled.append(updated)
            if self.event_repo:
                self.event_repo.record(
                    event_code=EventCode.TASK_RECONCILED,
                    category="recovery",
                    level=EventLevel.INFO,
                    entity_type="task",
                    entity_id=task.id,
                    payload={"previous_state": "INTERRUPTED", "new_state": target_state.value},
                )

        return reconciled

    def reconcile_unknown_send(self, task_id: str, verification_confirmed: Optional[bool] = None) -> str:
        """
        Reconcile unknown execution outcome.
        Fail-closed: without positive verification, transitions to MANUAL_REVIEW.
        """
        task = self.task_repo.get_by_id(task_id)
        if not task:
            return "NOT_FOUND"

        if verification_confirmed is True:
            self.task_repo.update_state(task_id=task.id, new_state=TaskState.COMPLETED, enforce_transition=False)
            return "COMPLETED"
        elif verification_confirmed is False:
            self.task_repo.update_state(task_id=task.id, new_state=TaskState.FAILED, enforce_transition=False)
            return "FAILED"
        else:
            self.task_repo.update_state(task_id=task.id, new_state=TaskState.MANUAL_REVIEW, enforce_transition=False)
            return "MANUAL_REVIEW"

    def recover_crashed_worker(self, worker_id: str) -> int:
        """
        Find tasks locked by a crashed worker and release them to INTERRUPTED.
        """
        conn = self.task_repo.db.get_connection()
        cursor = conn.execute(
            "SELECT id, lock_token FROM tasks WHERE worker_id = ? AND status = 'RUNNING';",
            (worker_id,),
        )
        rows = cursor.fetchall()
        recovered_count = 0
        now_iso = utc_now_iso()

        with self.task_repo.db.transaction() as tx_conn:
            for row in rows:
                tid = row["id"]
                tx_conn.execute(
                    """
                    UPDATE tasks SET
                        status = 'INTERRUPTED',
                        lock_token = NULL,
                        locked_at = NULL,
                        updated_at = ?
                    WHERE id = ?;
                    """,
                    (now_iso, tid),
                )
                recovered_count += 1

        return recovered_count
