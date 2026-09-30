"""Recovery service contract and foundation implementation for interrupted tasks and crashed workers."""

from abc import ABC, abstractmethod
from typing import List, Optional
from backend.domain.models import Task, utc_now_iso
from backend.domain.enums import TaskState, EventCode, EventLevel, ErrorCode
from backend.repositories.task_repo import TaskRepository
from backend.repositories.event_repo import EventRepository


class RecoveryService(ABC):
    """Contract for recovering interrupted tasks and reconciling unknown send results."""

    @abstractmethod
    def reconcile_interrupted(self, action: str = "RECONCILING") -> List[Task]:
        """Inspect and reconcile tasks marked as INTERRUPTED."""
        pass

    @abstractmethod
    def reconcile_unknown_send(self, task_id: str) -> str:
        """Inspect Instagram conversation state to determine if an unknown-send succeeded."""
        pass

    @abstractmethod
    def recover_crashed_worker(self, worker_id: str) -> int:
        """Handle crashed worker cleanup and task release."""
        pass


class DefaultRecoveryService(RecoveryService):
    """
    Phase 1 recovery engine foundation.
    Transitions INTERRUPTED tasks according to the state machine:
    INTERRUPTED -> RECONCILING / READY / MANUAL_REVIEW
    """

    def __init__(self, task_repo: TaskRepository, event_repo: Optional[EventRepository] = None):
        self.task_repo = task_repo
        self.event_repo = event_repo

    def reconcile_interrupted(self, action: str = "RECONCILING") -> List[Task]:
        """
        Transition INTERRUPTED tasks to the target reconciliation state.
        action can be: 'RECONCILING' | 'READY' | 'MANUAL_REVIEW'
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

    def reconcile_unknown_send(self, task_id: str) -> str:
        """
        Inspect Instagram conversation state to determine if an unknown-send succeeded.
        In Phase 1 foundation, moves task to MANUAL_REVIEW if unknown.
        """
        task = self.task_repo.get_by_id(task_id)
        if not task:
            return "NOT_FOUND"

        # Safe fail-closed rule: without browser verification, move to MANUAL_REVIEW
        self.task_repo.update_state(task_id=task.id, new_state=TaskState.MANUAL_REVIEW)
        return "MANUAL_REVIEW"

    def recover_crashed_worker(self, worker_id: str) -> int:
        """
        Find tasks locked by a crashed worker and release them to READY or INTERRUPTED.
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
