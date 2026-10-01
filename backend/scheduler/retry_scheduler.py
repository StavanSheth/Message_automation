"""Retry Scheduler and Policy Engine evaluating retryable tasks with exponential backoff and jitter."""

import random
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any, Tuple
from backend.domain.models import Task, ErrorRecord, utc_now_iso
from backend.domain.enums import TaskState, ErrorCode, RetryClass, EventCode, EventLevel
from backend.repositories.task_repo import TaskRepository
from backend.repositories.error_repo import ErrorRepository
from backend.repositories.event_repo import EventRepository
from backend.config.settings import AppSettings, get_settings
from backend.events.logger import get_logger

logger = get_logger("retry_scheduler")

# Errors that must NEVER be automatically retried
NON_RETRYABLE_CODES = {
    ErrorCode.PROFILE_MISMATCH,
    ErrorCode.PROFILE_NOT_FOUND,
    ErrorCode.ACCESS_PROHIBITED,
    ErrorCode.UNKNOWN_RESULT,
    ErrorCode.INVALID_DATA,
    ErrorCode.DUPLICATE_TASK,
    ErrorCode.RATE_LIMITED,
    ErrorCode.ACTION_BLOCKED,
    ErrorCode.CHALLENGE_REQUIRED,
}


class RetryPolicyEngine:
    """Classifies errors and calculates exponential backoff delay with jitter."""

    @staticmethod
    def classify_error(code: ErrorCode) -> Tuple[RetryClass, bool, int, float]:
        """
        Returns (RetryClass, retryable, max_attempts, base_delay_multiplier).
        """
        if code in NON_RETRYABLE_CODES:
            return RetryClass.UNKNOWN, False, 0, 0.0

        if code in (ErrorCode.NETWORK_OFFLINE, ErrorCode.TIMEOUT):
            return RetryClass.NETWORK, True, 3, 1.0

        if code == ErrorCode.BROWSER_CRASH:
            return RetryClass.BROWSER, True, 3, 0.5

        if code in (ErrorCode.UI_CHANGED, ErrorCode.DM_NOT_AVAILABLE):
            return RetryClass.TEMPORARY_INSTAGRAM, True, 2, 2.0

        if code == ErrorCode.MESSAGE_SEND_FAILED:
            return RetryClass.MESSAGE_SEND, True, 2, 1.0

        return RetryClass.UNKNOWN, True, 2, 1.0

    @staticmethod
    def calculate_delay(
        base_delay: float,
        attempt: int,
        multiplier: float = 1.0,
        enable_jitter: bool = True,
    ) -> float:
        """Calculate exponential backoff: base_delay * (2 ** (attempt - 1)) + jitter."""
        exp_factor = 2 ** max(0, attempt - 1)
        delay = base_delay * multiplier * exp_factor
        if enable_jitter:
            jitter = random.uniform(0.0, min(5.0, delay * 0.1))
            delay += jitter
        return delay


class RetryScheduler:
    """
    Evaluates tasks in RETRY_WAIT status, enforces backoff delays,
    and transitions ready tasks back to READY status or escalates to MANUAL_REVIEW.
    """

    def __init__(
        self,
        task_repo: TaskRepository,
        error_repo: Optional[ErrorRepository] = None,
        event_repo: Optional[EventRepository] = None,
        settings: Optional[AppSettings] = None,
    ):
        self.task_repo = task_repo
        self.error_repo = error_repo
        self.event_repo = event_repo
        self.settings = settings or get_settings()

    def evaluate_retries(self) -> List[Task]:
        """
        Scan tasks in RETRY_WAIT and transition eligible tasks to READY:
        - Non-retryable error -> escalate immediately to MANUAL_REVIEW.
        - Exceeded attempts -> escalate to MANUAL_REVIEW.
        - Backoff satisfied -> transition back to READY.
        """
        now = datetime.now(timezone.utc)
        base_delay = getattr(self.settings, "network_retry_delay", 30)
        global_max_retries = getattr(self.settings, "retry_limit", 3)
        enable_jitter = getattr(self.settings, "retry_jitter", True)

        conn = self.task_repo.db.get_connection()
        cursor = conn.execute("SELECT id FROM tasks WHERE status = 'RETRY_WAIT';")
        task_ids = [row["id"] for row in cursor.fetchall()]

        transitioned = []
        for tid in task_ids:
            task = self.task_repo.get_by_id(tid)
            if not task or task.status != TaskState.RETRY_WAIT:
                continue

            # Determine error context if available
            last_err = None
            if self.error_repo and task.last_error_id:
                last_err = self.error_repo.get_by_id(task.last_error_id)

            err_code = last_err.code if last_err else None
            if err_code in NON_RETRYABLE_CODES:
                logger.warning(
                    f"Task {task.id} has non-retryable error ({err_code.value}); escalating to MANUAL_REVIEW"
                )
                self.task_repo.update_state(task.id, TaskState.MANUAL_REVIEW, enforce_transition=False)
                continue

            r_class, retryable, class_max, multiplier = (
                RetryPolicyEngine.classify_error(err_code) if err_code else (RetryClass.NETWORK, True, global_max_retries, 1.0)
            )

            max_retries = min(global_max_retries, class_max)

            # Check max retry limit
            if task.attempt_count > max_retries:
                logger.warning(
                    f"Task {task.id} exceeded retry limit ({task.attempt_count} > {max_retries}); escalating to MANUAL_REVIEW"
                )
                self.task_repo.update_state(task.id, TaskState.MANUAL_REVIEW, enforce_transition=False)
                continue

            # Check backoff delay
            required_delay = RetryPolicyEngine.calculate_delay(
                base_delay=base_delay,
                attempt=task.attempt_count,
                multiplier=multiplier,
                enable_jitter=enable_jitter,
            )

            try:
                updated_dt = datetime.fromisoformat(task.updated_at.replace("Z", "+00:00"))
                if (now - updated_dt).total_seconds() >= required_delay:
                    updated = self.task_repo.update_state(task.id, TaskState.READY, enforce_transition=True)
                    transitioned.append(updated)
                    if self.event_repo:
                        self.event_repo.record(
                            event_code=EventCode.RETRY_SCHEDULED,
                            category="scheduler",
                            level=EventLevel.INFO,
                            entity_type="task",
                            entity_id=task.id,
                            payload={"attempt": task.attempt_count, "backoff_seconds": required_delay},
                        )
                    logger.info(f"Task {task.id} retry delay satisfied; transitioned back to READY")
            except Exception as e:
                logger.warning(f"Error evaluating retry for task {task.id}: {e}")

        return transitioned
