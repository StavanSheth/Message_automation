"""Concrete worker manager implementing WorkerManager contract."""

from typing import List, Optional, Dict, Any
from backend.workers.manager import WorkerManager
from backend.workers.worker import Worker
from backend.workers.worker_health import WorkerHealthMonitor
from backend.domain.models import WorkerRecord
from backend.domain.enums import WorkerStatus, WorkerMode, EventCode, EventLevel, TaskState
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
    Enforces concurrency limits, handles crash recovery, and manages worker lifecycles.
    """

    def __init__(
        self,
        task_repo: TaskRepository,
        event_repo: EventRepository,
        browser_manager: Optional[BrowserManager] = None,
        task_executor: Optional[Any] = None,
        worker_repo: Optional[Any] = None,
    ):
        self.task_repo = task_repo
        self.event_repo = event_repo
        self.browser_manager = browser_manager
        self.task_executor = task_executor
        self.worker_repo = worker_repo
        self.settings = get_settings()
        self._workers: Dict[str, Worker] = {}

    @property
    def effective_max_workers(self) -> int:
        """Authoritative maximum worker capacity, governed by BrowserManager if attached."""
        if self.browser_manager and hasattr(self.browser_manager, "effective_max_workers"):
            val = self.browser_manager.effective_max_workers
            if isinstance(val, int):
                return val
        return max(1, self.settings.max_workers)

    @property
    def effective_mode(self) -> WorkerMode:
        """Authoritative execution mode, governed by BrowserManager if attached."""
        if self.browser_manager and hasattr(self.browser_manager, "effective_mode"):
            val = self.browser_manager.effective_mode
            if isinstance(val, WorkerMode):
                return val
        try:
            return WorkerMode(self.settings.worker_mode)
        except (ValueError, AttributeError):
            return WorkerMode.SINGLE_BROWSER

    @property
    def active_count(self) -> int:
        """Number of active (non-stopped/crashed) workers."""
        return sum(
            1 for w in self._workers.values()
            if w.status not in (WorkerStatus.STOPPED, WorkerStatus.CRASHED)
        )

    def start_worker(self, mode: Optional[WorkerMode] = None) -> WorkerRecord:
        """Launch a new worker with an associated browser session. Enforces authoritative max_workers."""
        resolved_mode = mode or self.effective_mode
        if self.effective_mode == WorkerMode.SINGLE_BROWSER:
            resolved_mode = WorkerMode.SINGLE_BROWSER

        max_allowed = self.effective_max_workers
        if self.active_count >= max_allowed:
            raise RuntimeError(
                f"Cannot start worker: {self.active_count} active workers "
                f"already at max_workers={max_allowed}"
            )

        worker_id = generate_id("WKR")
        worker_code = f"worker-{len(self._workers) + 1}"

        session = None
        if self.browser_manager:
            session_inst = self.browser_manager.create_session(worker_id=worker_id)
            try:
                session_inst.start()
            except Exception:
                self.browser_manager.stop_session(session_inst.session_id)
                raise
            session = session_inst

        worker = Worker(
            worker_id=worker_id,
            worker_code=worker_code,
            mode=resolved_mode,
            task_repo=self.task_repo,
            event_repo=self.event_repo,
            session=session,
            heartbeat_interval=self.settings.worker_heartbeat_interval,
            stale_timeout=self.settings.worker_stale_timeout,
            worker_repo=self.worker_repo,
        )
        worker.start()
        self._workers[worker_id] = worker
        return worker.to_record()

    def stop_worker(self, worker_id: str) -> bool:
        """Stop a specific worker, clean up its browser session, and release any tasks."""
        worker = self._workers.get(worker_id)
        if not worker:
            return False

        worker.stop()
        if self.browser_manager and worker.session:
            try:
                self.browser_manager.stop_session(worker.session.session_id)
            except Exception as e:
                logger.warning(f"Error stopping browser session for worker {worker_id}: {e}")

        del self._workers[worker_id]
        return True

    def process_tasks(self, max_tasks: Optional[int] = None, adapter: Optional[Any] = None) -> int:
        """
        Dispatch available tasks to idle workers.
        Returns the number of tasks successfully processed.
        """
        if not self.task_executor:
            logger.warning("No task_executor configured on DefaultWorkerManager")
            return 0

        processed = 0
        for worker in list(self._workers.values()):
            if max_tasks is not None and processed >= max_tasks:
                break
            if worker.status == WorkerStatus.IDLE:
                success = worker.process_next_task(self.task_executor, adapter=adapter)
                if success:
                    processed += 1

        return processed

    def list_workers(self) -> List[WorkerRecord]:
        return [w.to_record() for w in self._workers.values()]

    def get_worker_status(self, worker_id: str) -> Optional[WorkerStatus]:
        worker = self._workers.get(worker_id)
        return worker.status if worker else None

    def get_worker(self, worker_id: str) -> Optional[Worker]:
        return self._workers.get(worker_id)

    def pause_worker(self, worker_id: str, reason: str = "") -> bool:
        worker = self._workers.get(worker_id)
        if not worker:
            return False
        return worker.pause(reason)

    def resume_worker(self, worker_id: str, reason: str = "") -> bool:
        worker = self._workers.get(worker_id)
        if not worker:
            return False
        return worker.resume(reason)

    def drain_worker(self, worker_id: str, reason: str = "") -> bool:
        worker = self._workers.get(worker_id)
        if not worker:
            return False
        return worker.drain(reason)

    def drain_all(self, reason: str = "System draining") -> int:
        """
        Drain all active workers:
        - Signal each active worker instance to drain
        - In-flight tasks complete safely
        """
        count = 0
        for worker in list(self._workers.values()):
            if worker.status not in (WorkerStatus.STOPPED, WorkerStatus.CRASHED):
                worker.drain(reason)
                count += 1
        return count

    def quarantine_worker(self, worker_id: str, reason: str = "") -> bool:
        worker = self._workers.get(worker_id)
        if not worker:
            return False
        return worker.quarantine(reason)

    def restart_worker(self, worker_id: str) -> Optional[WorkerRecord]:
        worker = self._workers.get(worker_id)
        if not worker:
            return None
        mode = worker.mode
        self.stop_worker(worker_id)
        return self.start_worker(mode=mode)

    def recover_stale_workers(self) -> int:
        """Find and recover stale workers (in-memory and persisted) by marking them crashed, releasing locks, and cleaning up."""
        from datetime import datetime, timezone
        recovered_ids = set()

        # 1. Recover in-memory workers
        stale = WorkerHealthMonitor.find_stale_workers(list(self._workers.values()))
        for worker in stale:
            logger.warning(f"Worker {worker.worker_id} is stale, marking as crashed")
            worker.status = WorkerStatus.CRASHED
            if self.worker_repo:
                try:
                    self.worker_repo.update(worker.to_record())
                except Exception as e:
                    logger.warning(f"Failed to update worker {worker.worker_id} in repo: {e}")

            # Release any task the worker was holding and mark INTERRUPTED or route to RECONCILIATION
            if worker.current_task_id:
                try:
                    t = self.task_repo.get_by_id(worker.current_task_id)
                    if t and t.status in (TaskState.SENDING, TaskState.VERIFYING):
                        if hasattr(self, "reconciliation_service") and self.reconciliation_service:
                            self.reconciliation_service.enter_reconciliation(
                                task_id=t.id,
                                worker_id=worker.worker_id,
                                reason="stale_worker_during_send",
                            )
                        else:
                            self.task_repo.update_state(t.id, TaskState.MANUAL_REVIEW, worker_id=worker.worker_id, enforce_transition=False)
                    else:
                        self.task_repo.update_state(
                            worker.current_task_id,
                            TaskState.INTERRUPTED,
                            enforce_transition=False,
                        )
                        lid = (t.lease_id or t.lock_token) if t else None
                        if lid:
                            self.task_repo.release_lease(worker.current_task_id, lid, worker.worker_id)
                except Exception as e:
                    logger.warning(f"Could not safely recover task {worker.current_task_id}: {e}")

            self.event_repo.record(
                event_code=EventCode.WORKER_CRASHED,
                category="worker",
                level=EventLevel.ERROR,
                entity_type="worker",
                entity_id=worker.worker_id,
                payload={"reason": "heartbeat_stale", "task_id": worker.current_task_id},
            )
            self.stop_worker(worker.worker_id)
            recovered_ids.add(worker.worker_id)

        # 2. Recover persisted workers in DB that are not in memory or died unexpectedly
        if self.worker_repo:
            try:
                all_persisted = self.worker_repo.list_all()
                now = datetime.now(timezone.utc)
                stale_threshold = getattr(self.settings, "worker_stale_timeout", 60)
                for prec in all_persisted:
                    if prec.id in recovered_ids:
                        continue
                    if prec.status in (WorkerStatus.IDLE, WorkerStatus.BUSY):
                        if prec.last_heartbeat:
                            try:
                                hb_dt = datetime.fromisoformat(prec.last_heartbeat.replace("Z", "+00:00"))
                                if (now - hb_dt).total_seconds() > stale_threshold:
                                    logger.warning(f"Persisted worker {prec.id} is stale in DB, marking crashed")
                                    prec.status = WorkerStatus.CRASHED
                                    self.worker_repo.update(prec)
                                    if prec.current_task_id:
                                        t = self.task_repo.get_by_id(prec.current_task_id)
                                        if t and t.status in (TaskState.SENDING, TaskState.VERIFYING):
                                            if hasattr(self, "reconciliation_service") and self.reconciliation_service:
                                                self.reconciliation_service.enter_reconciliation(
                                                    task_id=t.id,
                                                    worker_id=prec.id,
                                                    reason="stale_worker_during_send",
                                                )
                                            else:
                                                self.task_repo.update_state(t.id, TaskState.MANUAL_REVIEW, worker_id=prec.id, enforce_transition=False)
                                        else:
                                            self.task_repo.update_state(
                                                prec.current_task_id,
                                                TaskState.INTERRUPTED,
                                                enforce_transition=False,
                                            )
                                            lid = (t.lease_id or t.lock_token) if t else None
                                            if lid:
                                                self.task_repo.release_lease(prec.current_task_id, lid, prec.id)
                                    self.event_repo.record(
                                        event_code=EventCode.WORKER_CRASHED,
                                        category="worker",
                                        level=EventLevel.ERROR,
                                        entity_type="worker",
                                        entity_id=prec.id,
                                        payload={"reason": "heartbeat_stale_db", "task_id": prec.current_task_id},
                                    )
                                    if prec.id in self._workers:
                                        self.stop_worker(prec.id)
                                    recovered_ids.add(prec.id)
                            except Exception as e:
                                logger.warning(f"Error checking heartbeat for worker {prec.id}: {e}")
            except Exception as e:
                logger.warning(f"Error scanning persisted workers for staleness: {e}")

        return len(recovered_ids)


    def shutdown_all(self) -> None:
        """Stop all workers and release all resources."""
        for worker_id in list(self._workers.keys()):
            self.stop_worker(worker_id)
