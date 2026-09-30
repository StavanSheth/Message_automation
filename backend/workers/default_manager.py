"""Concrete worker manager implementing WorkerManager contract."""

from typing import List, Optional, Dict
from backend.workers.manager import WorkerManager
from backend.workers.worker import Worker
from backend.workers.worker_health import WorkerHealthMonitor
from backend.domain.models import WorkerRecord
from backend.domain.enums import WorkerStatus, WorkerMode, EventCode, EventLevel
from backend.browser.manager import BrowserManager
from backend.repositories.task_repo import TaskRepository
from backend.repositories.event_repo import EventRepository
from backend.config.settings import get_settings
from backend.events.correlation import generate_id
from backend.events.logger import get_logger

logger = get_logger("default_worker_manager")


class DefaultWorkerManager(WorkerManager):
    """
    Concrete WorkerManager that coordinates Worker instances with BrowserManager.
    Enforces concurrency limits, handles crash recovery, and manages heartbeats.
    """

    def __init__(
        self,
        task_repo: TaskRepository,
        event_repo: EventRepository,
        browser_manager: Optional[BrowserManager] = None,
    ):
        self.task_repo = task_repo
        self.event_repo = event_repo
        self.browser_manager = browser_manager
        self.settings = get_settings()
        self._workers: Dict[str, Worker] = {}

    def start_worker(self, mode: WorkerMode) -> WorkerRecord:
        """Launch a new worker with a browser session."""
        worker_id = generate_id("WKR")
        worker_code = f"worker-{len(self._workers) + 1}"

        session = None
        if self.browser_manager:
            session_inst = self.browser_manager.create_session(worker_id=worker_id)
            session_inst.start()
            session = session_inst

        worker = Worker(
            worker_id=worker_id,
            worker_code=worker_code,
            mode=mode,
            task_repo=self.task_repo,
            event_repo=self.event_repo,
            session=session,
            heartbeat_interval=self.settings.worker_heartbeat_interval,
            stale_timeout=self.settings.worker_stale_timeout,
        )
        worker.start()
        self._workers[worker_id] = worker
        return worker.to_record()

    def stop_worker(self, worker_id: str) -> bool:
        """Stop a specific worker and clean up its browser session."""
        worker = self._workers.get(worker_id)
        if not worker:
            return False

        worker.stop()
        if self.browser_manager and worker.session:
            self.browser_manager.stop_session(worker.session.session_id)

        del self._workers[worker_id]
        return True

    def list_workers(self) -> List[WorkerRecord]:
        return [w.to_record() for w in self._workers.values()]

    def get_worker_status(self, worker_id: str) -> Optional[WorkerStatus]:
        worker = self._workers.get(worker_id)
        return worker.status if worker else None

    def get_worker(self, worker_id: str) -> Optional[Worker]:
        return self._workers.get(worker_id)

    def recover_stale_workers(self) -> int:
        """Find and recover stale workers by marking them crashed and stopping."""
        stale = WorkerHealthMonitor.find_stale_workers(list(self._workers.values()))
        recovered = 0
        for worker in stale:
            logger.warning(f"Worker {worker.worker_id} is stale, marking as crashed")
            worker.status = WorkerStatus.CRASHED
            self.event_repo.record(
                event_code=EventCode.WORKER_CRASHED,
                category="worker",
                level=EventLevel.ERROR,
                entity_type="worker",
                entity_id=worker.worker_id,
                payload={"reason": "heartbeat_stale"},
            )
            self.stop_worker(worker.worker_id)
            recovered += 1
        return recovered

    def shutdown_all(self) -> None:
        """Stop all workers."""
        for worker_id in list(self._workers.keys()):
            self.stop_worker(worker_id)
