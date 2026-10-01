"""Worker recovery handler for stale heartbeats and crashed worker instances."""

from typing import List, Optional
from backend.domain.models import WorkerRecord
from backend.domain.enums import WorkerStatus, EventCode, EventLevel
from backend.repositories.worker_repo import WorkerRepository
from backend.repositories.task_repo import TaskRepository
from backend.repositories.event_repo import EventRepository
from backend.recovery.task_recovery import TaskRecoveryHandler
from backend.events.logger import get_logger

logger = get_logger("worker_recovery")


class WorkerRecoveryHandler:
    """Detects crashed/stale workers and recovers held tasks safely."""

    def __init__(
        self,
        worker_repo: WorkerRepository,
        task_repo: TaskRepository,
        task_recovery_handler: TaskRecoveryHandler,
        event_repo: Optional[EventRepository] = None,
    ):
        self.worker_repo = worker_repo
        self.task_repo = task_repo
        self.task_recovery_handler = task_recovery_handler
        self.event_repo = event_repo

    def recover_worker(self, worker_id: str, reason: str = "heartbeat_timeout") -> int:
        """
        Recover a specific worker:
        1. Mark worker as CRASHED/STOPPED
        2. Release/recover any task held by the worker
        """
        wrec = self.worker_repo.get_by_id(worker_id)
        if not wrec:
            return 0

        self.worker_repo.update_status(worker_id, WorkerStatus.CRASHED)

        if self.event_repo:
            self.event_repo.record(
                event_code=EventCode.WORKER_CRASHED,
                category="recovery",
                level=EventLevel.WARNING,
                entity_type="worker",
                entity_id=worker_id,
                payload={"reason": reason},
            )

        recovered_tasks = 0
        if wrec.current_task_id:
            held_task = self.task_repo.get_by_id(wrec.current_task_id)
            if held_task:
                self.task_recovery_handler.recover_task(held_task, reason=f"worker_crash_{worker_id}")
                recovered_tasks += 1
            self.worker_repo.update_current_task(worker_id, None)

        return recovered_tasks
