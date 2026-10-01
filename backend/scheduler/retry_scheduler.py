"""Retry Scheduler for evaluating retryable tasks and handling backoff transitions."""

from datetime import datetime, timezone, timedelta
from typing import List, Optional
from backend.domain.models import Task, utc_now_iso
from backend.domain.enums import TaskState
from backend.repositories.task_repo import TaskRepository
from backend.config.settings import AppSettings, get_settings
from backend.events.logger import get_logger

logger = get_logger("retry_scheduler")


class RetryScheduler:
    """
    Evaluates tasks in RETRY_WAIT status, enforces backoff delays,
    and transitions ready tasks back to READY status or escalates to MANUAL_REVIEW.
    """

    def __init__(
        self,
        task_repo: TaskRepository,
        settings: Optional[AppSettings] = None,
    ):
        self.task_repo = task_repo
        self.settings = settings or get_settings()

    def evaluate_retries(self) -> List[Task]:
        """
        Scan tasks in RETRY_WAIT and transition eligible tasks to READY:
        - If attempt_count > retry_limit -> escalate to MANUAL_REVIEW.
        - If updated_at + network_retry_delay <= now -> transition to READY.
        Returns list of transitioned tasks.
        """
        now = datetime.now(timezone.utc)
        retry_delay = getattr(self.settings, "network_retry_delay", 30)
        max_retries = getattr(self.settings, "retry_limit", 3)

        conn = self.task_repo.db.get_connection()
        cursor = conn.execute("SELECT id FROM tasks WHERE status = 'RETRY_WAIT';")
        task_ids = [row["id"] for row in cursor.fetchall()]

        transitioned = []
        for tid in task_ids:
            task = self.task_repo.get_by_id(tid)
            if not task or task.status != TaskState.RETRY_WAIT:
                continue

            # Check max retry limit
            if task.attempt_count > max_retries:
                logger.warning(
                    f"Task {task.id} exceeded retry limit ({task.attempt_count} > {max_retries}); escalating to MANUAL_REVIEW"
                )
                self.task_repo.update_state(task.id, TaskState.MANUAL_REVIEW, enforce_transition=False)
                continue

            # Check retry delay
            try:
                updated_dt = datetime.fromisoformat(task.updated_at.replace("Z", "+00:00"))
                if (now - updated_dt).total_seconds() >= retry_delay:
                    updated = self.task_repo.update_state(task.id, TaskState.READY, enforce_transition=True)
                    transitioned.append(updated)
                    logger.info(f"Task {task.id} retry delay satisfied; transitioned back to READY")
            except Exception as e:
                logger.warning(f"Error evaluating retry for task {task.id}: {e}")

        return transitioned
