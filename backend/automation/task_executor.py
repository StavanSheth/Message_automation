"""Task execution foundation for browser and source operations."""

from typing import Optional, Dict, Any
from backend.domain.models import Task, utc_now_iso
from backend.domain.enums import (
    TaskState,
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
)
from backend.automation.execution_context import ExecutionContext
from backend.browser.session import BrowserSessionInstance
from backend.browser.exceptions import BrowserTimeoutError, BrowserCrashError
from backend.sources.base import SourceAdapter
from backend.application.source_service import SourceService
from backend.repositories.task_repo import TaskRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.error_repo import ErrorRepository
from backend.events.correlation import generate_id
from backend.events.logger import get_logger

logger = get_logger("task_executor")


class TaskExecutor:
    """
    Executes browser-backed source synchronization and foundation tasks.
    Enforces Phase 2 boundaries: does NOT perform Instagram messaging.
    """

    def __init__(
        self,
        task_repo: TaskRepository,
        event_repo: EventRepository,
        error_repo: ErrorRepository,
        source_service: Optional[SourceService] = None,
    ):
        self.task_repo = task_repo
        self.event_repo = event_repo
        self.error_repo = error_repo
        self.source_service = source_service

    def execute_source_sync(
        self,
        task: Task,
        context: ExecutionContext,
        adapter: SourceAdapter,
        session: Optional[BrowserSessionInstance] = None,
    ) -> bool:
        """
        Execute source validation, open, read, and synchronization for a task.
        """
        context.task_id = task.id
        if session:
            context.session_id = session.session_id

        logger.info(
            "Starting task source sync execution",
            task_id=task.id,
            worker_id=context.worker_id,
            correlation_id=context.correlation_id,
        )

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
                "source_identifier": adapter.source_identifier,
            },
        )

        try:
            # 1. Update task to RUNNING
            self.task_repo.update_state(task.id, TaskState.RUNNING, worker_id=context.worker_id)

            # 2. Execute synchronization via SourceService
            if not self.source_service:
                raise AutomationError("SourceService is required for task execution", code=ErrorCode.INTERNAL_ERROR)

            sync_run = self.source_service.sync_source(adapter)

            # 3. Transition to COMPLETED
            self.task_repo.update_state(task.id, TaskState.COMPLETED, worker_id=context.worker_id)

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
            self._handle_failure(task, context, ErrorCode.TIMEOUT, str(e), retryable=True)
            return False

        except (BrowserCrashError, SourceAccessError) as e:
            code = getattr(e, "code", ErrorCode.SOURCE_UNAVAILABLE)
            logger.error(f"Source access/browser error in task {task.id}: {e}")
            self._handle_failure(task, context, code, str(e), retryable=False)
            return False

        except ConflictError as e:
            logger.warning(f"Conflict detected during execution of task {task.id}: {e}")
            self._handle_failure(task, context, ErrorCode.SYNC_CONFLICT, str(e), retryable=False, state=TaskState.MANUAL_REVIEW)
            return False

        except Exception as e:
            logger.error(f"Unexpected error executing task {task.id}: {e}")
            self._handle_failure(task, context, ErrorCode.INTERNAL_ERROR, str(e), retryable=False)
            return False

    def _handle_failure(
        self,
        task: Task,
        context: ExecutionContext,
        error_code: ErrorCode,
        message: str,
        retryable: bool,
        state: Optional[TaskState] = None,
    ) -> None:
        """Record structured error and advance task state."""
        from backend.domain.models import ErrorRecord
        err = ErrorRecord(
            id=generate_id("ERR"),
            code=error_code,
            message=message,
            severity=ErrorSeverity.HIGH if not retryable else ErrorSeverity.MEDIUM,
            retryable=retryable,
            attempt=task.attempt_count + 1,
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
            )
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
                "error_code": error_code.value,
                "error_message": message,
                "retryable": retryable,
                "next_state": next_state.value,
            },
        )
