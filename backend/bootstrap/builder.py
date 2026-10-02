"""Authoritative production bootstrap builder and dependency graph validation."""

import os
from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple, List

from backend.config.settings import AppSettings, get_settings
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.database.backup import DatabaseBackupService

# Repositories
from backend.repositories.task_repo import TaskRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.followup_repo import FollowupRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.error_repo import ErrorRepository
from backend.repositories.verification_result_repo import VerificationResultRepository
from backend.repositories.execution_identity_repo import ExecutionIdentityRepository
from backend.repositories.account_repo import AccountRepository
from backend.repositories.worker_repo import WorkerRepository
from backend.repositories.browser_session_repo import BrowserSessionRepository
from backend.repositories.system_control_repo import SystemControlRepository
from backend.repositories.cooldown_repo import CooldownRepository
from backend.repositories.reconciliation_repo import ReconciliationRepository
from backend.repositories.manual_review_repo import ManualReviewRepository

# Services
from backend.browser.instagram.auth_validator import InstagramAuthValidator
from backend.application.throttling_service import ThrottlingService
from backend.reconciliation.service import ReconciliationService
from backend.browser.instagram.automation_service import InstagramAutomationService
from backend.application.execution_service import ExecutionService
from backend.automation.task_executor import TaskExecutor
from backend.browser.manager import BrowserManager
from backend.workers.default_manager import DefaultWorkerManager
from backend.application.lifecycle import ApplicationLifecycleManager
from backend.application.control_service import ApplicationControlService
from backend.scheduler.task_dispatcher import TaskDispatcher
from backend.scheduler.scheduler import Scheduler
from backend.application.retention_service import RetentionService
from backend.application.manual_review_service import ManualReviewService
from backend.application.status_service import StatusService
from backend.application.metrics_service import MetricsService

from backend.domain.enums import SystemState, EventCode, EventLevel
from backend.events.logger import get_logger

logger = get_logger("bootstrap")


@dataclass
class ProductionApp:
    """Complete, validated production application runtime graph."""
    settings: AppSettings
    db: DatabaseManager
    # Repositories
    task_repo: TaskRepository
    message_repo: MessageRepository
    contact_repo: ContactRepository
    followup_repo: FollowupRepository
    event_repo: EventRepository
    error_repo: ErrorRepository
    verification_repo: VerificationResultRepository
    execution_identity_repo: ExecutionIdentityRepository
    account_repo: AccountRepository
    worker_repo: WorkerRepository
    session_repo: BrowserSessionRepository
    system_control_repo: SystemControlRepository
    cooldown_repo: CooldownRepository
    reconciliation_repo: ReconciliationRepository
    manual_review_repo: ManualReviewRepository
    # Services
    auth_validator: InstagramAuthValidator
    throttling_service: ThrottlingService
    reconciliation_service: ReconciliationService
    automation_service: InstagramAutomationService
    execution_service: ExecutionService
    task_executor: TaskExecutor
    browser_manager: BrowserManager
    worker_manager: DefaultWorkerManager
    lifecycle_manager: ApplicationLifecycleManager
    control_service: ApplicationControlService
    task_dispatcher: TaskDispatcher
    scheduler: Scheduler
    retention_service: RetentionService
    manual_review_service: ManualReviewService
    status_service: StatusService
    metrics_service: MetricsService
    backup_service: DatabaseBackupService

    def validate_dependency_graph(self) -> Tuple[bool, List[str]]:
        """
        Authoritatively validate that the production messaging dependency graph is intact:
        Scheduler -> TaskDispatcher -> Worker -> TaskExecutor -> ExecutionService -> AutomationService
        And that all mandatory components exist and are properly cross-wired.
        """
        missing: List[str] = []

        components = {
            "TaskDispatcher": self.task_dispatcher,
            "ExecutionService": self.execution_service,
            "ThrottlingService": self.throttling_service,
            "ReconciliationService": self.reconciliation_service,
            "WorkerManager": self.worker_manager,
            "Scheduler": self.scheduler,
            "BrowserManager": self.browser_manager,
            "AccountRepository": self.account_repo,
            "TaskRepository": self.task_repo,
            "MessageRepository": self.message_repo,
            "EventRepository": self.event_repo,
            "ExecutionIdentityRepository": self.execution_identity_repo,
            "SystemControlRepository": self.system_control_repo,
            "AuthValidator": self.auth_validator,
            "LifecycleManager": self.lifecycle_manager,
            "ControlService": self.control_service,
            "DatabaseBackupService": self.backup_service,
        }

        for name, comp in components.items():
            if comp is None:
                missing.append(f"Missing mandatory component: {name}")

        if self.scheduler and getattr(self.scheduler, "task_dispatcher", None) is not self.task_dispatcher:
            missing.append("Scheduler.task_dispatcher must be the authoritative TaskDispatcher")
        if self.worker_manager and getattr(self.worker_manager, "execution_service", None) is not self.execution_service:
            missing.append("WorkerManager.execution_service must be the authoritative ExecutionService")
        if self.task_executor and getattr(self.task_executor, "execution_service", None) is not self.execution_service:
            missing.append("TaskExecutor.execution_service must be the authoritative ExecutionService")
        if self.task_dispatcher and getattr(self.task_dispatcher, "throttling_service", None) is not self.throttling_service:
            missing.append("TaskDispatcher.throttling_service must be the authoritative ThrottlingService")

        return len(missing) == 0, missing

    def start(self) -> Dict[str, Any]:
        """
        Start sequence (Section 5):
        1. Validate complete production dependency graph.
        2. If invalid -> transition to DEGRADED, NEVER RUNNING.
        3. If valid -> invoke control_service.start().
        """
        is_valid, errors = self.validate_dependency_graph()
        if not is_valid:
            logger.error(f"Cannot start production app: invalid dependency graph: {errors}")
            if self.control_service:
                self.control_service._transition_to(SystemState.STARTING, "Startup initiated")
                self.control_service._transition_to(SystemState.DEGRADED, f"Dependency validation failed: {errors}")
            if self.event_repo:
                try:
                    self.event_repo.record(
                        event_code=EventCode.TASK_STATE_CHANGED,
                        category="system",
                        level=EventLevel.ERROR,
                        payload={"status": "degraded", "errors": errors},
                    )
                except Exception:
                    pass
            return {"status": "error", "message": f"Dependency validation failed: {errors}"}

        return self.control_service.start()

    def stop(self) -> None:
        """Gracefully stop the application and release resources."""
        if self.control_service:
            self.control_service.stop("Application stopped by operator")
        if self.lifecycle_manager:
            self.lifecycle_manager.graceful_shutdown()

    def pause(self, reason: str = "Operator requested pause") -> bool:
        return self.control_service.pause(reason) if self.control_service else False

    def resume(self, reason: str = "Operator requested resume") -> bool:
        return self.control_service.resume(reason) if self.control_service else False


def build_production_app(
    db_path: Optional[str] = None,
    settings: Optional[AppSettings] = None,
    apply_migrations: bool = True,
    **overrides,
) -> ProductionApp:
    """
    Authoritative factory constructing and wiring the complete production application graph.
    Enforces Phase 1-5 structural invariants.
    """
    app_settings = settings or get_settings()
    app_settings.validate_runtime_config()

    database = overrides.get("db") or DatabaseManager(db_path or app_settings.database_path)
    if apply_migrations:
        runner = MigrationRunner(database)
        runner.apply_pending()

    # 1. Repositories
    task_repo = overrides.get("task_repo") or TaskRepository(database)
    message_repo = overrides.get("message_repo") or MessageRepository(database)
    contact_repo = overrides.get("contact_repo") or ContactRepository(database)
    followup_repo = overrides.get("followup_repo") or FollowupRepository(database)
    event_repo = overrides.get("event_repo") or EventRepository(database)
    error_repo = overrides.get("error_repo") or ErrorRepository(database)
    verification_repo = overrides.get("verification_repo") or VerificationResultRepository(database)
    exec_id_repo = overrides.get("execution_identity_repo") or ExecutionIdentityRepository(database)
    account_repo = overrides.get("account_repo") or AccountRepository(database)
    worker_repo = overrides.get("worker_repo") or WorkerRepository(database)
    session_repo = overrides.get("session_repo") or BrowserSessionRepository(database)
    system_control_repo = overrides.get("system_control_repo") or SystemControlRepository(database)
    cooldown_repo = overrides.get("cooldown_repo") or CooldownRepository(database)
    reconciliation_repo = overrides.get("reconciliation_repo") or ReconciliationRepository(database)
    manual_review_repo = overrides.get("manual_review_repo") or ManualReviewRepository(database)

    # 2. Base Services
    backup_service = overrides.get("backup_service") or DatabaseBackupService(database)
    auth_validator = overrides.get("auth_validator") or InstagramAuthValidator()

    throttling_service = overrides.get("throttling_service") or ThrottlingService(
        cooldown_repo=cooldown_repo,
        account_repo=account_repo,
        event_repo=event_repo,
        settings=app_settings,
    )

    reconciliation_service = overrides.get("reconciliation_service") or ReconciliationService(
        reconciliation_repo=reconciliation_repo,
        task_repo=task_repo,
        message_repo=message_repo,
        manual_review_repo=manual_review_repo,
        event_repo=event_repo,
    )

    automation_service = overrides.get("automation_service") or InstagramAutomationService(
        task_repo=task_repo,
        contact_repo=contact_repo,
        message_repo=message_repo,
        followup_repo=followup_repo,
        event_repo=event_repo,
        error_repo=error_repo,
        verification_repo=verification_repo,
        settings=app_settings,
        auth_validator=auth_validator,
        reconciliation_service=reconciliation_service,
        cooldown_repo=cooldown_repo,
        throttling_service=throttling_service,
    )

    execution_service = overrides.get("execution_service") or ExecutionService(
        task_repo=task_repo,
        message_repo=message_repo,
        automation_service=automation_service,
        reconciliation_service=reconciliation_service,
        manual_review_repo=manual_review_repo,
        event_repo=event_repo,
        execution_identity_repo=exec_id_repo,
        throttling_service=throttling_service,
        worker_repo=worker_repo,
        account_repo=account_repo,
        auth_validator=auth_validator,
    )

    task_executor = overrides.get("task_executor") or TaskExecutor(
        task_repo=task_repo,
        event_repo=event_repo,
        error_repo=error_repo,
        instagram_service=automation_service,
        execution_service=execution_service,
    )

    browser_manager = overrides.get("browser_manager") or BrowserManager(
        settings=app_settings,
        event_repo=event_repo,
        session_repo=session_repo,
        driver_factory=overrides.get("driver_factory"),
    )

    worker_manager = overrides.get("worker_manager") or DefaultWorkerManager(
        task_repo=task_repo,
        event_repo=event_repo,
        browser_manager=browser_manager,
        task_executor=task_executor,
        worker_repo=worker_repo,
        execution_service=execution_service,
    )

    retention_service = overrides.get("retention_service") or RetentionService(
        db=database,
        settings=app_settings,
    )

    manual_review_service = overrides.get("manual_review_service") or ManualReviewService(
        manual_review_repo=manual_review_repo,
        task_repo=task_repo,
        message_repo=message_repo,
        event_repo=event_repo,
        reconciliation_repo=reconciliation_repo,
    )

    lifecycle_manager = overrides.get("lifecycle_manager") or ApplicationLifecycleManager(
        db=database,
        task_repo=task_repo,
        event_repo=event_repo,
        manual_review_repo=manual_review_repo,
        reconciliation_service=reconciliation_service,
        browser_manager=browser_manager,
        worker_manager=worker_manager,
        retention_service=retention_service,
    )

    control_service = overrides.get("control_service") or ApplicationControlService(
        lifecycle_manager=lifecycle_manager,
        worker_manager=worker_manager,
        browser_manager=browser_manager,
        event_repo=event_repo,
        settings=app_settings,
        system_control_repo=system_control_repo,
    )

    task_dispatcher = overrides.get("task_dispatcher") or TaskDispatcher(
        task_repo=task_repo,
        worker_manager=worker_manager,
        throttling_service=throttling_service,
        control_service=control_service,
        account_repo=account_repo,
        manual_review_repo=manual_review_repo,
        reconciliation_repo=reconciliation_repo,
        cooldown_repo=cooldown_repo,
        auth_validator=auth_validator,
    )

    status_service = overrides.get("status_service") or StatusService(
        db=database,
        control_service=control_service,
        worker_manager=worker_manager,
        browser_manager=browser_manager,
        account_repo=account_repo,
        cooldown_repo=cooldown_repo,
    )

    metrics_service = overrides.get("metrics_service") or MetricsService(
        db=database,
    )

    scheduler = overrides.get("scheduler") or Scheduler(
        task_repo=task_repo,
        followup_repo=followup_repo,
        contact_repo=contact_repo,
        worker_manager=worker_manager,
        control_service=control_service,
        throttling_service=throttling_service,
        task_dispatcher=task_dispatcher,
        auth_validator=auth_validator,
        retention_service=retention_service,
    )

    # Link scheduler to control service and lifecycle manager
    control_service.scheduler = scheduler
    lifecycle_manager.scheduler = scheduler
    worker_manager.task_dispatcher = task_dispatcher

    return ProductionApp(
        settings=app_settings,
        db=database,
        task_repo=task_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        followup_repo=followup_repo,
        event_repo=event_repo,
        error_repo=error_repo,
        verification_repo=verification_repo,
        execution_identity_repo=exec_id_repo,
        account_repo=account_repo,
        worker_repo=worker_repo,
        session_repo=session_repo,
        system_control_repo=system_control_repo,
        cooldown_repo=cooldown_repo,
        reconciliation_repo=reconciliation_repo,
        manual_review_repo=manual_review_repo,
        auth_validator=auth_validator,
        throttling_service=throttling_service,
        reconciliation_service=reconciliation_service,
        automation_service=automation_service,
        execution_service=execution_service,
        task_executor=task_executor,
        browser_manager=browser_manager,
        worker_manager=worker_manager,
        lifecycle_manager=lifecycle_manager,
        control_service=control_service,
        task_dispatcher=task_dispatcher,
        scheduler=scheduler,
        retention_service=retention_service,
        manual_review_service=manual_review_service,
        status_service=status_service,
        metrics_service=metrics_service,
        backup_service=backup_service,
    )
