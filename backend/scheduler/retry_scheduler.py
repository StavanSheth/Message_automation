"""Retry Scheduler and Policy Engine evaluating retryable tasks with exponential backoff and jitter."""

import random
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any, Tuple
from backend.domain.models import Task, ErrorRecord, utc_now_iso
from backend.domain.enums import TaskState, ErrorCode, RetryClass, ErrorResolution, EventCode, EventLevel
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
    ErrorCode.SESSION_EXPIRED,
    ErrorCode.CAPTCHA_REQUIRED,
    ErrorCode.RECONCILIATION_FAILED,
    ErrorCode.UI_CHANGED,
}


class RetryPolicyEngine:
    """Classifies errors and calculates exponential backoff delay with jitter."""

    def __init__(
        self,
        base_delay: float = 30.0,
        max_delay: float = 300.0,
        max_attempts: int = 3,
        jitter_factor: float = 0.1,
    ):
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.max_attempts = max_attempts
        self.jitter_factor = jitter_factor

    @classmethod
    def classify_category(cls, code: Any) -> RetryClass:
        """Map error code or string to one of the 11 RetryClass categories."""
        raw = str(code.value if hasattr(code, "value") else code).upper()

        if any(k in raw for k in ("PROFILE_MISMATCH", "WRONG_ACCOUNT")):
            return RetryClass.PROFILE_MISMATCH
        if any(k in raw for k in ("ACCESS_PROHIBITED", "ACTION_BLOCKED", "CHALLENGE", "CHECKPOINT")):
            return RetryClass.ACCESS_BLOCKED
        if any(k in raw for k in ("RATE_LIMIT", "429", "TRY_AGAIN_LATER")):
            return RetryClass.RATE_LIMIT
        if any(k in raw for k in ("LOGIN_REQUIRED", "SESSION_EXPIRED", "CAPTCHA")):
            return RetryClass.AUTHENTICATION
        if any(k in raw for k in ("PROFILE_NOT_FOUND", "NAVIGATION_TIMEOUT", "NAV_TIMEOUT")):
            return RetryClass.NAVIGATION
        if any(k in raw for k in ("UNKNOWN_SEND_RESULT", "UNKNOWN_RESULT", "SEND_TIMEOUT", "MESSAGE_SEND")):
            return RetryClass.MESSAGE_SEND
        if any(k in raw for k in ("VERIFICATION_UNKNOWN", "OCR_LOW_CONFIDENCE", "VERIFICATION")):
            return RetryClass.VERIFICATION
        if any(k in raw for k in ("NETWORK_OFFLINE", "TIMEOUT", "ECONNRESET", "NET_TIMEOUT")):
            return RetryClass.NETWORK
        if any(k in raw for k in ("BROWSER_CRASH", "SESSION_DISCONNECTED", "BROWSER")):
            return RetryClass.BROWSER
        if any(k in raw for k in ("DM_NOT_AVAILABLE", "INSTAGRAM_500", "SERVER_BUSY")):
            return RetryClass.TEMPORARY_INSTAGRAM
        return RetryClass.UNKNOWN

    @classmethod
    def classify_error(cls, code: Any) -> Tuple[RetryClass, bool, int, float]:
        """
        Returns (RetryClass, retryable, max_attempts, base_delay_multiplier).
        Authoritative mapping across all 11 error domains.
        """
        # If code is ErrorCode enum directly
        if isinstance(code, ErrorCode):
            if code == ErrorCode.PROFILE_MISMATCH:
                return RetryClass.PROFILE_MISMATCH, False, 0, 0.0
            if code == ErrorCode.ACCESS_PROHIBITED:
                return RetryClass.ACCESS_BLOCKED, False, 0, 0.0
            if code in (ErrorCode.ACTION_BLOCKED, ErrorCode.RATE_LIMITED):
                return RetryClass.RATE_LIMIT, False, 0, 0.0
            if code in (ErrorCode.CHALLENGE_REQUIRED, ErrorCode.SESSION_EXPIRED, ErrorCode.CAPTCHA_REQUIRED):
                return RetryClass.AUTHENTICATION, False, 0, 0.0
            if code == ErrorCode.PROFILE_NOT_FOUND:
                return RetryClass.NAVIGATION, False, 0, 0.0
            if code in (ErrorCode.UNKNOWN_RESULT, ErrorCode.RECONCILIATION_FAILED):
                return RetryClass.MESSAGE_SEND, False, 0, 0.0
            if code in NON_RETRYABLE_CODES:
                return RetryClass.UNKNOWN, False, 0, 0.0
            if code in (ErrorCode.NETWORK_OFFLINE, ErrorCode.TIMEOUT):
                return RetryClass.NETWORK, True, 3, 1.0
            if code == ErrorCode.BROWSER_CRASH:
                return RetryClass.BROWSER, True, 3, 0.5
            if code == ErrorCode.DM_NOT_AVAILABLE:
                return RetryClass.TEMPORARY_INSTAGRAM, True, 2, 2.0
            if code == ErrorCode.MESSAGE_SEND_FAILED:
                return RetryClass.MESSAGE_SEND, True, 2, 1.0
            if code == ErrorCode.OCR_LOW_CONFIDENCE:
                return RetryClass.VERIFICATION, True, 2, 1.0

        cat = cls.classify_category(code)
        resolution = cls.get_resolution(cat)
        retryable = resolution == ErrorResolution.RETRYABLE
        max_attempts = 3 if retryable else 0
        mult = 1.0 if retryable else 0.0
        return cat, retryable, max_attempts, mult

    @classmethod
    def get_resolution(cls, target: Any) -> ErrorResolution:
        """Resolve an ErrorCode, RetryClass, or error string to its authoritative handling action."""
        if isinstance(target, RetryClass):
            if target in (RetryClass.MESSAGE_SEND, RetryClass.VERIFICATION):
                return ErrorResolution.RECONCILIATION_REQUIRED
            if target in (
                RetryClass.AUTHENTICATION,
                RetryClass.ACCESS_BLOCKED,
                RetryClass.PROFILE_MISMATCH,
                RetryClass.UNKNOWN,
            ):
                return ErrorResolution.MANUAL_REVIEW_REQUIRED
            return ErrorResolution.RETRYABLE

        if isinstance(target, ErrorCode):
            if target in (ErrorCode.UNKNOWN_RESULT, ErrorCode.MESSAGE_SEND_FAILED):
                return ErrorResolution.RECONCILIATION_REQUIRED
            if target in (
                ErrorCode.PROFILE_MISMATCH,
                ErrorCode.ACCESS_PROHIBITED,
                ErrorCode.ACTION_BLOCKED,
                ErrorCode.RATE_LIMITED,
                ErrorCode.CHALLENGE_REQUIRED,
                ErrorCode.SESSION_EXPIRED,
                ErrorCode.UI_CHANGED,
                ErrorCode.RECONCILIATION_FAILED,
            ):
                return ErrorResolution.MANUAL_REVIEW_REQUIRED
            if target in (ErrorCode.PROFILE_NOT_FOUND, ErrorCode.INVALID_DATA, ErrorCode.DUPLICATE_TASK):
                return ErrorResolution.NON_RETRYABLE
            return ErrorResolution.RETRYABLE

        # Fallback for strings
        cat = cls.classify_category(target)
        return cls.get_resolution(cat)

    def compute_backoff_delay(self, attempt: int) -> float:
        """Calculate capped exponential backoff delay with jitter."""
        return self.calculate_delay(
            base_delay=self.base_delay,
            attempt=attempt,
            max_delay=self.max_delay,
        )

    def should_retry(self, error: Any, attempt: int) -> bool:
        """Evaluate if an error at given attempt count should be retried."""
        resolution = self.get_resolution(error)
        if resolution != ErrorResolution.RETRYABLE:
            return False
        return attempt < self.max_attempts

    @staticmethod
    def calculate_delay(
        base_delay: float,
        attempt: int,
        multiplier: float = 1.0,
        enable_jitter: bool = True,
        max_delay: float = 300.0,
    ) -> float:
        """Calculate exponential backoff: min(max_delay, base_delay * multiplier * 2^(attempt - 1)) + jitter."""
        exp_factor = 2 ** max(0, attempt - 1)
        raw_delay = base_delay * multiplier * exp_factor
        delay = min(max_delay, raw_delay)
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
            max_delay = getattr(self.settings, "retry_max_delay", 300.0)
            required_delay = RetryPolicyEngine.calculate_delay(
                base_delay=base_delay,
                attempt=task.attempt_count,
                multiplier=multiplier,
                enable_jitter=enable_jitter,
                max_delay=max_delay,
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
