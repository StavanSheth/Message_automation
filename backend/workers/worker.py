"""Worker instance with continuous heartbeat, task claiming, and lifecycle management."""

import time
import threading
from datetime import datetime, timezone
from typing import Optional, Any
from backend.domain.models import WorkerRecord, Task, utc_now_iso
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
    Maintains a continuous background heartbeat while active and thread-safe state.
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

        self._lock = threading.Lock()
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_stop = threading.Event()

    def start(self) -> None:
        """Start the worker, launch browser session, and start continuous background heartbeat."""
        with self._lock:
            self.status = WorkerStatus.IDLE
            self.last_heartbeat = utc_now_iso()

        if self.session and not self.session.is_alive():
            self.session.start()

        self._start_heartbeat()

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
        """Stop the worker, release any claimed task, stop heartbeat, close browser session."""
        self._stop_heartbeat()
        self.release_current_task()

        if self.session:
            try:
                self.session.stop()
            except Exception as e:
                logger.warning(f"Error stopping browser session for worker {self.worker_id}: {e}")

        with self._lock:
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

    def _start_heartbeat(self) -> None:
        """Launch background daemon thread for continuous heartbeat."""
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"heartbeat-{self.worker_id}",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def _stop_heartbeat(self) -> None:
        """Signal and wait for background heartbeat thread termination."""
        self._heartbeat_stop.set()
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=1.0)
        self._heartbeat_thread = None

    def _heartbeat_loop(self) -> None:
        """Periodic background heartbeat update."""
        while not self._heartbeat_stop.wait(timeout=self.heartbeat_interval):
            with self._lock:
                if self.status in (WorkerStatus.STOPPED, WorkerStatus.CRASHED):
                    break
                self.last_heartbeat = utc_now_iso()

    def heartbeat(self) -> str:
        """Record an explicit heartbeat timestamp."""
        with self._lock:
            self.last_heartbeat = utc_now_iso()
            return self.last_heartbeat

    def is_stale(self) -> bool:
        """Check if heartbeat is older than stale_timeout seconds."""
        try:
            with self._lock:
                ts = self.last_heartbeat
            last = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            return (now - last).total_seconds() > self.stale_timeout
        except Exception:
            return True

    def claim_task(self, task_id: str) -> bool:
        """Attempt to atomically claim a task for execution."""
        with self._lock:
            if self.status != WorkerStatus.IDLE:
                return False

        lock_token = generate_id("LOCK")
        claimed = self.task_repo.claim_task(task_id, self.worker_id, lock_token)
        if claimed:
            with self._lock:
                self.current_task_id = task_id
                self._lock_token = lock_token
                self.status = WorkerStatus.BUSY
                self.last_heartbeat = utc_now_iso()
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
        tid = None
        token = None
        with self._lock:
            tid = self.current_task_id
            token = self._lock_token
            self.current_task_id = None
            self._lock_token = None
            if self.status not in (WorkerStatus.STOPPED, WorkerStatus.CRASHED):
                self.status = WorkerStatus.IDLE
            self.last_heartbeat = utc_now_iso()

        if tid and token:
            try:
                self.task_repo.release_task(tid, token)
            except Exception as e:
                logger.warning(f"Error releasing task {tid} for worker {self.worker_id}: {e}")

    def process_next_task(self, executor: Any, adapter: Optional[Any] = None) -> bool:
        """
        Execute next ready task:
        1. Verify worker is IDLE.
        2. Query next ready task.
        3. Atomically claim task.
        4. Create ExecutionContext.
        5. Invoke TaskExecutor.
        6. Release lock and return to IDLE.
        Guarantees worker is never left stuck in BUSY.
        """
        with self._lock:
            if self.status != WorkerStatus.IDLE:
                return False

        tasks = self.task_repo.list_ready(limit=1)
        if not tasks:
            return False

        target_task = tasks[0]
        if not self.claim_task(target_task.id):
            return False

        context = ExecutionContext(
            worker_id=self.worker_id,
            session_id=self.session.session_id if self.session else None,
            task_id=target_task.id,
        )

        try:
            # Re-fetch claimed task to have the fresh locked state
            claimed_task = self.task_repo.get_by_id(target_task.id) or target_task
            result = executor.execute_source_sync(
                task=claimed_task,
                context=context,
                adapter=adapter,
                session=self.session,
            )
            return bool(result)
        except Exception as e:
            logger.error(f"Worker {self.worker_id} encountered error executing task {target_task.id}: {e}")
            return False
        finally:
            # Ensure task lock is released and worker returned to IDLE
            self.release_current_task()

    def to_record(self) -> WorkerRecord:
        with self._lock:
            return WorkerRecord(
                id=self.worker_id,
                worker_code=self.worker_code,
                mode=self.mode,
                status=self.status,
                current_task_id=self.current_task_id,
                last_heartbeat=self.last_heartbeat,
            )
