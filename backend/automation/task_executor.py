"""Task execution foundation for browser and source operations."""

from typing import Optional, Dict, Any
from backend.domain.models import Task, ErrorRecord, utc_now_iso
from backend.domain.enums import (
    TaskState,
    TaskType,
    ErrorCode,
    EventCode,
    EventLevel,
    ErrorSeverity,
)
from backend.domain.errors import (
    AutomationError,
    SourceAccessError,
    ConflictError,
    ValidationError,
    TaskStateError,
)
from backend.automation.execution_context import ExecutionContext
from backend.browser.session import BrowserSessionInstance
from backend.browser.exceptions import BrowserTimeoutError, BrowserCrashError
from backend.sources.base import SourceAdapter
from backend.application.source_service import SourceService
from backend.repositories.task_repo import TaskRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.error_repo import ErrorRepository
from backend.browser.instagram.automation_service import InstagramAutomationService
from backend.events.correlation import generate_id
from backend.events.logger import get_logger

logger = get_logger("task_executor")


class TaskExecutor:
    """
    Executes browser-backed automation tasks including source sync,
    Instagram messaging, and follow-up executions.
    Dispatches according to TaskType.
    """

    def __init__(
        self,
        task_repo: TaskRepository,
        event_repo: EventRepository,
        error_repo: ErrorRepository,
        source_service: Optional[SourceService] = None,
        instagram_service: Optional[InstagramAutomationService] = None,
        max_retries: int = 3,
    ):
        self.task_repo = task_repo
        self.event_repo = event_repo
        self.error_repo = error_repo
        self.source_service = source_service
        self.instagram_service = instagram_service
        self.max_retries = max_retries

    def execute_task(
        self,
        task: Task,
        context: ExecutionContext,
        session: Optional[BrowserSessionInstance] = None,
        adapter: Optional[SourceAdapter] = None,
    ) -> bool:
        """
        Primary execution dispatch method based on TaskType:
        - TaskType.MESSAGE -> execute_instagram_message
        - TaskType.FOLLOW_UP_1 -> execute_instagram_message
        - TaskType.FOLLOW_UP_2 -> execute_instagram_message
        - Default -> execute_source_sync
        """
        if task.type in (TaskType.MESSAGE, TaskType.FOLLOW_UP_1, TaskType.FOLLOW_UP_2):
            if self.instagram_service:
                return self.execute_instagram_message(task, context, session)
        return self.execute_source_sync(task, context, adapter, session)

    def execute_instagram_message(
        self,
        task: Task,
        context: ExecutionContext,
        session: Optional[BrowserSessionInstance] = None,
    ) -> bool:
        """
        Execute an Instagram message or follow-up task.
        Verifies atomic claim state, worker ownership, and delegates to InstagramAutomationService.
        """
        context.task_id = task.id
        if session:
            context.session_id = session.session_id

        # ── Guard: task must already be claimed (RUNNING + lock_token) ──
        current = self.task_repo.get_by_id(task.id)
        if not current:
            logger.error(f"Task {task.id} not found in repository")
            return False

        if current.status != TaskState.RUNNING:
            logger.error(
                f"Task {task.id} is {current.status.value}, not RUNNING. "
                "Tasks must be claimed via claim_task() before execution."
            )
            return False

        if not current.lock_token:
            logger.error(f"Task {task.id} has no lock_token. Tasks must be claimed before execution.")
            return False

        if context.worker_id and current.worker_id != context.worker_id:
            logger.error(
                f"Task {task.id} claimed by worker {current.worker_id}, "
                f"but execution requested by {context.worker_id}."
            )
            return False

        if not self.instagram_service:
            logger.error("InstagramAutomationService is required for messaging task execution")
            self._handle_failure(current, context, ErrorCode.INTERNAL_ERROR, "Instagram service not configured", retryable=False)
            return False

        if not session or not session.is_alive():
            logger.error(f"Session not available for task {task.id}")
            self._handle_failure(current, context, ErrorCode.BROWSER_CRASH, "Browser session not available", retryable=True)
            return False

        # Check retry limit
        if current.attempt_count > self.max_retries:
            logger.warning(f"Task {task.id} exceeded maximum retries ({current.attempt_count} > {self.max_retries}); escalating to manual review")
            self._handle_failure(
                current, context, ErrorCode.INTERNAL_ERROR,
                f"Exceeded max retries ({current.attempt_count})",
                retryable=False, state=TaskState.MANUAL_REVIEW,
            )
            return False

        try:
            success = self.instagram_service.execute_messaging_task(
                task=current,
                session=session,
                worker_id=context.worker_id,
                correlation_id=context.correlation_id,
            )

            # Re-verify task ownership before considering execution finished
            verify = self.task_repo.get_by_id(task.id)
            if verify and verify.worker_id and verify.worker_id != context.worker_id:
                logger.error(f"Task {task.id} ownership stolen by {verify.worker_id}")
                return False

            return success
        except BrowserTimeoutError as e:
            logger.error(f"Task {task.id} timed out during execution: {e}")
            self._handle_failure(current, context, ErrorCode.TIMEOUT, str(e), retryable=True)
            return False
        except BrowserCrashError as e:
            logger.error(f"Browser crashed during task {task.id}: {e}")
            self._handle_failure(current, context, ErrorCode.BROWSER_CRASH, str(e), retryable=True, state=TaskState.INTERRUPTED)
            return False
        except Exception as e:
            logger.error(f"Unexpected error executing task {task.id}: {e}")
            self._handle_failure(current, context, ErrorCode.INTERNAL_ERROR, str(e), retryable=False)
            return False
        finally:
            # Release lock in all cases if lock is still held
            try:
                latest = self.task_repo.get_by_id(task.id)
                if latest and latest.lock_token == current.lock_token:
                    self.task_repo.release_task(task.id, current.lock_token)
            except Exception as e:
                logger.warning(f"Failed to release task {task.id} lock in finally block: {e}")

    def execute_source_sync(
        self,
        task: Task,
        context: ExecutionContext,
        adapter: Optional[SourceAdapter] = None,
        session: Optional[BrowserSessionInstance] = None,
    ) -> bool:
        """
        Execute source sync for a task that has already been atomically claimed.
        The task MUST be in RUNNING state with a valid lock_token and worker_id
        set by TaskRepository.claim_task().
        """
        context.task_id = task.id
        if session:
            context.session_id = session.session_id

        # ── Guard: task must already be claimed (RUNNING + lock_token) ──
        current = self.task_repo.get_by_id(task.id)
        if not current:
            logger.error(f"Task {task.id} not found in repository")
            return False

        if current.status != TaskState.RUNNING:
            logger.error(
                f"Task {task.id} is {current.status.value}, not RUNNING. "
                "Tasks must be claimed via claim_task() before execution."
            )
            return False

        if not current.lock_token:
            logger.error(
                f"Task {task.id} has no lock_token. "
                "Tasks must be atomically claimed before execution."
            )
            return False

        if context.worker_id and current.worker_id != context.worker_id:
            logger.error(
                f"Task {task.id} claimed by worker {current.worker_id}, "
                f"but execution requested by {context.worker_id}."
            )
            return False

        logger.info(
            "Starting task source sync execution",
            task_id=task.id,
            worker_id=context.worker_id,
            correlation_id=context.correlation_id,
        )

        source_ident = str(adapter.source_identifier) if adapter else "default_source"
        self.event_repo.record(
            event_code=EventCode.TASK_EXECUTION_STARTED,
            category="automation",
            level=EventLevel.INFO,
            entity_type="task",
            entity_id=task.id,
            payload={
                "run_id": context.run_id,
                "worker_id": context.worker_id,
                "session_id": context.session_id,
                "correlation_id": context.correlation_id,
                "source_identifier": source_ident,
                "lock_token": current.lock_token,
                "attempt_count": current.attempt_count,
            },
        )

        completed_successfully = False
        try:
            # Execute synchronization via SourceService
            if not self.source_service:
                raise AutomationError(
                    code=ErrorCode.INTERNAL_ERROR,
                    message="SourceService is required for task execution",
                )

            if not adapter:
                raise AutomationError(
                    code=ErrorCode.SOURCE_UNAVAILABLE,
                    message="SourceAdapter is required for source sync execution",
                )

            sync_run = self.source_service.sync_source(adapter)

            # Verify ownership has not changed before completing
            verify = self.task_repo.get_by_id(task.id)
            if not verify or verify.status != TaskState.RUNNING or verify.lock_token != current.lock_token:
                logger.error(f"Task {task.id} lost ownership or was interrupted during execution")
                return False

            # Transition to COMPLETED
            self.task_repo.update_state(
                task.id, TaskState.COMPLETED, worker_id=context.worker_id
            )
            completed_successfully = True

            self.event_repo.record(
                event_code=EventCode.TASK_EXECUTION_COMPLETED,
                category="automation",
                level=EventLevel.INFO,
                entity_type="task",
                entity_id=task.id,
                payload={
                    "run_id": context.run_id,
                    "records_read": sync_run.records_read,
                    "records_written": sync_run.records_written,
                    "conflicts": sync_run.conflicts,
                },
            )
            return True

        except (BrowserTimeoutError, TimeoutError) as e:
            logger.error(f"Task {task.id} timed out during source execution: {e}")
            self._handle_failure(
                current, context, ErrorCode.TIMEOUT, str(e), retryable=True
            )
            return False

        except BrowserCrashError as e:
            logger.error(f"Browser crash during task {task.id}: {e}")
            self._handle_failure(
                current, context, ErrorCode.BROWSER_CRASH, str(e),
                retryable=True, state=TaskState.INTERRUPTED,
            )
            return False

        except SourceAccessError as e:
            code = getattr(e, "code", ErrorCode.SOURCE_UNAVAILABLE)
            logger.error(f"Source access error in task {task.id}: {e}")
            self._handle_failure(current, context, code, str(e), retryable=False)
            return False

        except ConflictError as e:
            logger.warning(f"Conflict detected during execution of task {task.id}: {e}")
            self._handle_failure(
                current, context, ErrorCode.SYNC_CONFLICT, str(e),
                retryable=False, state=TaskState.MANUAL_REVIEW,
            )
            return False

        except Exception as e:
            logger.error(f"Unexpected error executing task {task.id}: {e}")
            self._handle_failure(
                current, context, ErrorCode.INTERNAL_ERROR, str(e), retryable=False
            )
            return False

        finally:
            # Release lock in all cases if lock is still held
            try:
                latest = self.task_repo.get_by_id(task.id)
                if latest and latest.lock_token == current.lock_token:
                    self.task_repo.release_task(task.id, current.lock_token)
            except Exception as e:
                logger.warning(f"Failed to release task {task.id} lock in finally block: {e}")

    def _handle_failure(
        self,
        task: Task,
        context: ExecutionContext,
        error_code: ErrorCode,
        message: str,
        retryable: bool,
        state: Optional[TaskState] = None,
    ) -> None:
        """Record structured error, advance task state, and release lock."""
        err = ErrorRecord(
            id=generate_id("ERR"),
            code=error_code,
            message=message,
            severity=ErrorSeverity.HIGH if not retryable else ErrorSeverity.MEDIUM,
            retryable=retryable,
            attempt=task.attempt_count,
            task_id=task.id,
        )
        error_rec = self.error_repo.record(err)

        next_state = state or (TaskState.RETRY_WAIT if retryable else TaskState.FAILED)
        try:
            self.task_repo.update_state(
                task.id,
                next_state,
                worker_id=context.worker_id,
                last_error_id=error_rec.id,
                enforce_transition=False,
            )
        except TaskStateError as e:
            logger.error(f"Could not transition task {task.id} to {next_state.value}: {e}")

        # Release lock so task can be re-claimed
        if task.lock_token:
            try:
                self.task_repo.release_task(task.id, task.lock_token)
            except Exception:
                pass

        self.event_repo.record(
            event_code=EventCode.TASK_EXECUTION_FAILED,
            category="automation",
            level=EventLevel.ERROR,
            entity_type="task",
            entity_id=task.id,
            payload={
                "run_id": context.run_id,
                "worker_id": context.worker_id,
                "session_id": context.session_id,
                "correlation_id": context.correlation_id,
                "error_code": error_code.value,
                "error_message": message,
                "retryable": retryable,
                "next_state": next_state.value,
            },
        )
