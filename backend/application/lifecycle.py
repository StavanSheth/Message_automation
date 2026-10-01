"""Application lifecycle manager implementing idempotent startup recovery and graceful shutdown."""

from typing import Optional, Dict, Any, List
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.task_repo import TaskRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.manual_review_repo import ManualReviewRepository
from backend.reconciliation.service import ReconciliationService
from backend.recovery.service import DefaultRecoveryService
from backend.recovery.task_recovery import TaskRecoveryHandler
from backend.recovery.worker_recovery import WorkerRecoveryHandler
from backend.browser.manager import BrowserManager
from backend.scheduler.scheduler import Scheduler
from backend.workers.default_manager import DefaultWorkerManager
from backend.domain.enums import EventCode, EventLevel, TaskState
from backend.events.logger import get_logger

logger = get_logger("application_lifecycle")


class ApplicationLifecycleManager:
    """
    Coordinates production application lifecycle:
    - Idempotent startup recovery across database, leases, stale workers, and reconciliation.
    - Graceful, orderly shutdown releasing leases, stopping threads, closing sessions.
    """

    def __init__(
        self,
        db: DatabaseManager,
        task_repo: TaskRepository,
        event_repo: Optional[EventRepository] = None,
        manual_review_repo: Optional[ManualReviewRepository] = None,
        reconciliation_service: Optional[ReconciliationService] = None,
        recovery_service: Optional[DefaultRecoveryService] = None,
        browser_manager: Optional[BrowserManager] = None,
        worker_manager: Optional[DefaultWorkerManager] = None,
        scheduler: Optional[Scheduler] = None,
    ):
        self.db = db
        self.task_repo = task_repo
        self.event_repo = event_repo
        self.manual_review_repo = manual_review_repo
        self.reconciliation_service = reconciliation_service
        self.recovery_service = recovery_service
        self.browser_manager = browser_manager
        self.worker_manager = worker_manager
        self.scheduler = scheduler

    def startup_recovery(self) -> Dict[str, Any]:
        """
        Execute idempotent startup recovery sequence:
        1. Open DB & validate schema (apply pending migrations).
        2. Detect stale workers.
        3. Detect and recover expired leases.
        4. Detect interrupted tasks and recover safe tasks / escalate unsafe.
        5. Report summary.
        """
        logger.info("Executing startup recovery sequence...")
        summary = {
            "migrations_applied": 0,
            "expired_leases_recovered": 0,
            "stale_workers_recovered": 0,
            "interrupted_tasks_recovered": 0,
        }

        # 1. Validate schema
        try:
            migration_runner = MigrationRunner(self.db)
            applied = migration_runner.apply_pending()
            summary["migrations_applied"] = len(applied)
        except Exception as e:
            logger.error(f"Error validating database schema: {e}")

        # 2. Detect and recover expired leases
        try:
            task_recovery_handler = TaskRecoveryHandler(
                task_repo=self.task_repo,
                reconciliation_service=self.reconciliation_service,
                event_repo=self.event_repo,
            )
            recovered_leases = task_recovery_handler.recover_expired_leases()
            summary["expired_leases_recovered"] = recovered_leases
        except Exception as e:
            logger.error(f"Error recovering expired leases during startup: {e}")

        # 3. Detect and recover stale workers
        try:
            if self.worker_manager and hasattr(self.worker_manager, "recover_stale_workers"):
                summary["stale_workers_recovered"] = self.worker_manager.recover_stale_workers()
        except Exception as e:
            logger.error(f"Error recovering stale workers during startup: {e}")

        # 4. Mark tasks left in RUNNING as INTERRUPTED, and recover safe tasks
        try:
            interrupted_count = self.task_repo.mark_running_as_interrupted()
            if self.recovery_service:
                recovered_tasks = self.recovery_service.recover_interrupted_tasks()
                summary["interrupted_tasks_recovered"] = len(recovered_tasks)
            else:
                summary["interrupted_tasks_recovered"] = interrupted_count
        except Exception as e:
            logger.error(f"Error recovering interrupted tasks during startup: {e}")

        if self.event_repo:
            self.event_repo.record(
                event_code=EventCode.TASK_RECOVERED,
                category="system",
                level=EventLevel.INFO,
                payload=summary,
            )

        logger.info(f"Startup recovery completed successfully: {summary}")
        return summary

    def graceful_shutdown(self) -> None:
        """
        Execute orderly shutdown sequence:
        1. Stop accepting new tasks (pause scheduler).
        2. Stop scheduler loop.
        3. Release worker task leases.
        4. Stop workers.
        5. Close browser sessions.
        6. Close database connection.
        """
        logger.info("Executing graceful shutdown sequence...")

        # 1 & 2. Stop scheduler
        if self.scheduler:
            try:
                self.scheduler.pause()
                self.scheduler.stop()
                logger.info("Scheduler stopped cleanly")
            except Exception as e:
                logger.warning(f"Error stopping scheduler: {e}")

        # 3 & 4. Release active leases, stop workers
        if self.worker_manager:
            try:
                # Release all active task leases held by workers before stopping
                for worker in self.worker_manager.list_workers():
                    if worker.current_task_id:
                        try:
                            self.task_repo.unlock_task(worker.current_task_id)
                        except Exception as e:
                            logger.warning(f"Error releasing lease for task {worker.current_task_id}: {e}")
                if hasattr(self.worker_manager, "stop_all"):
                    self.worker_manager.stop_all()
                elif hasattr(self.worker_manager, "shutdown_all"):
                    self.worker_manager.shutdown_all()
                logger.info("Workers stopped cleanly")
            except Exception as e:
                logger.warning(f"Error stopping workers: {e}")

        # 5. Close browser sessions
        if self.browser_manager:
            try:
                self.browser_manager.close_all()
                logger.info("Browser sessions closed cleanly")
            except Exception as e:
                logger.warning(f"Error closing browser sessions: {e}")

        # 6. Flush event logs and close DB
        try:
            self.db.close()
            logger.info("Database closed cleanly")
        except Exception as e:
            logger.warning(f"Error closing database: {e}")

        logger.info("Graceful shutdown completed")
