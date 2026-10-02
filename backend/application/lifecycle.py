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
        retention_service: Optional[Any] = None,
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
        self.retention_service = retention_service

    def run_retention_sweep(self) -> Dict[str, int]:
        """Execute controlled historical data pruning through lifecycle manager."""
        if self.retention_service:
            return self.retention_service.cleanup_expired_data()
        from backend.application.retention_service import RetentionService
        ret_svc = RetentionService(self.db)
        return ret_svc.cleanup_expired_data()

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

        # 4. Mark tasks left in RUNNING as INTERRUPTED, and route SENDING/VERIFYING to RECONCILING
        try:
            # Check tasks in SENDING or VERIFYING that were interrupted
            conn = self.db.get_connection()
            cur = conn.execute("SELECT id, worker_id, status FROM tasks WHERE status IN ('SENDING', 'VERIFYING');")
            uncertain_tasks = cur.fetchall()
            for r in uncertain_tasks:
                tid, wid, st = r[0], r[1], r[2]
                if self.reconciliation_service:
                    self.reconciliation_service.enter_reconciliation(
                        task_id=tid,
                        reason=f"startup_recovery_interrupted_{st.lower()}",
                        worker_id=wid,
                    )
                else:
                    self.task_repo.update_state(tid, TaskState.RECONCILING, enforce_transition=False)

            interrupted_count = self.task_repo.mark_running_as_interrupted()
            if self.recovery_service:
                recovered_tasks = self.recovery_service.recover_interrupted_tasks()
                summary["interrupted_tasks_recovered"] = len(recovered_tasks)
            else:
                summary["interrupted_tasks_recovered"] = interrupted_count
        except Exception as e:
            logger.error(f"Error recovering interrupted tasks during startup: {e}")

        # 5. Clean up orphaned execution identities left in RUNNING
        try:
            from backend.repositories.execution_identity_repo import ExecutionIdentityRepository
            exec_repo = ExecutionIdentityRepository(self.db)
            conn = self.db.get_connection()
            cur = conn.execute("SELECT execution_key FROM execution_identities WHERE state = 'RUNNING';")
            running_keys = [r[0] for r in cur.fetchall()]
            for k in running_keys:
                exec_repo.update_state(k, state="RECONCILIATION", outcome="orphaned_startup_recovery")
            summary["orphaned_execution_identities"] = len(running_keys)
        except Exception as e:
            logger.debug(f"Error checking execution identities during startup: {e}")
            summary["orphaned_execution_identities"] = 0

        # 6. Detect unresolved reconciliations
        try:
            conn = self.db.get_connection()
            cur = conn.execute("SELECT COUNT(*) FROM reconciliations WHERE state = 'PENDING';")
            summary["unresolved_reconciliations"] = cur.fetchone()[0]
        except Exception as e:
            summary["unresolved_reconciliations"] = 0

        # 7. Detect manual reviews requiring attention
        try:
            conn = self.db.get_connection()
            cur = conn.execute("SELECT COUNT(*) FROM manual_reviews WHERE status = 'PENDING';")
            summary["manual_review_items"] = cur.fetchone()[0]
        except Exception as e:
            summary["manual_review_items"] = 0

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
                # If worker was in SENDING or VERIFYING, route through RECONCILIATION
                for worker in self.worker_manager.list_workers():
                    if worker.current_task_id:
                        try:
                            t = self.task_repo.get_by_id(worker.current_task_id)
                            if t:
                                if t.status in (TaskState.SENDING, TaskState.VERIFYING):
                                    if self.reconciliation_service:
                                        self.reconciliation_service.enter_reconciliation(
                                            task_id=t.id,
                                            worker_id=worker.id,
                                            reason="shutdown_during_send",
                                        )
                                    else:
                                        self.task_repo.update_state(t.id, TaskState.MANUAL_REVIEW, worker_id=worker.id, enforce_transition=False)
                                elif t.lease_id:
                                    self.task_repo.release_lease(t.id, t.lease_id, worker.id)
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
