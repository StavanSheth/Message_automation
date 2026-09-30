"""Worker instance with heartbeat, task claiming, and lifecycle management."""

import time
from typing import Optional
from backend.domain.models import WorkerRecord, utc_now_iso
from backend.domain.enums import (
    WorkerMode, WorkerStatus, TaskState,
    EventCode, EventLevel, ErrorCode,
)
from backend.browser.session import BrowserSessionInstance
from backend.automation.execution_context import ExecutionContext
from backend.repositories.task_repo import TaskRepository
from backend.repositories.event_repo import EventRepository
from backend.events.correlation import generate_id
from backend.events.logger import get_logger

logger = get_logger("worker")


class Worker:
    """
    A single worker instance that owns a browser session and executes tasks.
    Tracks heartbeat, status, and current task.
    """

    def __init__(
        self,
        worker_id: str,
        worker_code: str,
        mode: WorkerMode,
        task_repo: TaskRepository,
        event_repo: EventRepository,
        session: Optional[BrowserSessionInstance] = None,
        heartbeat_interval: int = 15,
        stale_timeout: int = 60,
    ):
        self.worker_id = worker_id
        self.worker_code = worker_code
        self.mode = mode
        self.status = WorkerStatus.IDLE
        self.task_repo = task_repo
        self.event_repo = event_repo
        self.session = session
        self.heartbeat_interval = heartbeat_interval
        self.stale_timeout = stale_timeout
        self.current_task_id: Optional[str] = None
        self.last_heartbeat: str = utc_now_iso()
        self._lock_token: Optional[str] = None

    def start(self) -> None:
        """Start the worker and its browser session."""
        self.status = WorkerStatus.IDLE
        self.last_heartbeat = utc_now_iso()
        if self.session and not self.session.is_alive():
            self.session.start()
        self.event_repo.record(
            event_code=EventCode.WORKER_STARTED,
            category="worker",
            level=EventLevel.INFO,
            entity_type="worker",
            entity_id=self.worker_id,
            payload={"worker_code": self.worker_code, "mode": self.mode.value},
        )
        logger.info("Worker started", worker_id=self.worker_id, mode=self.mode.value)

    def stop(self) -> None:
        """Stop the worker, release any claimed task, close browser session."""
        if self.current_task_id and self._lock_token:
            try:
                self.task_repo.release_task(self.current_task_id, self._lock_token)
            except Exception as e:
                logger.warning(f"Failed to release task on worker stop: {e}")
        self.current_task_id = None
        self._lock_token = None

        if self.session:
            try:
                self.session.stop()
            except Exception as e:
                logger.warning(f"Error stopping browser session for worker {self.worker_id}: {e}")

        self.status = WorkerStatus.STOPPED
        self.event_repo.record(
            event_code=EventCode.WORKER_STOPPED,
            category="worker",
            level=EventLevel.INFO,
            entity_type="worker",
            entity_id=self.worker_id,
            payload={"worker_code": self.worker_code},
        )
        logger.info("Worker stopped", worker_id=self.worker_id)

    def heartbeat(self) -> str:
        """Record a heartbeat timestamp."""
        self.last_heartbeat = utc_now_iso()
        return self.last_heartbeat

    def is_stale(self) -> bool:
        """Check if heartbeat is older than stale_timeout seconds."""
        from datetime import datetime, timezone
        try:
            last = datetime.fromisoformat(self.last_heartbeat.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            return (now - last).total_seconds() > self.stale_timeout
        except Exception:
            return True

    def claim_task(self, task_id: str) -> bool:
        """Attempt to atomically claim a task for execution."""
        if self.status not in (WorkerStatus.IDLE,):
            return False

        lock_token = generate_id("LOCK")
        claimed = self.task_repo.claim_task(task_id, self.worker_id, lock_token)
        if claimed:
            self.current_task_id = task_id
            self._lock_token = lock_token
            self.status = WorkerStatus.BUSY
            self.heartbeat()
            self.event_repo.record(
                event_code=EventCode.TASK_CLAIMED,
                category="worker",
                level=EventLevel.INFO,
                entity_type="task",
                entity_id=task_id,
                payload={"worker_id": self.worker_id, "lock_token": lock_token},
            )
        return claimed

    def release_current_task(self) -> None:
        """Release the currently held task and return to IDLE."""
        if self.current_task_id and self._lock_token:
            self.task_repo.release_task(self.current_task_id, self._lock_token)
        self.current_task_id = None
        self._lock_token = None
        self.status = WorkerStatus.IDLE
        self.heartbeat()

    def to_record(self) -> WorkerRecord:
        return WorkerRecord(
            id=self.worker_id,
            worker_code=self.worker_code,
            mode=self.mode,
            status=self.status,
            current_task_id=self.current_task_id,
            last_heartbeat=self.last_heartbeat,
        )
