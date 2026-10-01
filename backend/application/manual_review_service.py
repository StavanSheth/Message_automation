"""Manual review service managing operator review lifecycle and explicit resolutions."""

from typing import List, Optional, Dict, Any
from backend.domain.models import ManualReviewItem
from backend.domain.enums import TaskState, MessageState, EventCode, EventLevel
from backend.repositories.manual_review_repo import ManualReviewRepository
from backend.repositories.task_repo import TaskRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.event_repo import EventRepository
from backend.events.logger import get_logger

logger = get_logger("manual_review_service")

VALID_RESOLUTIONS = {
    "CONFIRMED_SENT",
    "CONFIRMED_NOT_SENT",
    "RETRY_ALLOWED",
    "CANCELLED",
}


class ManualReviewService:
    """
    Operator Control Service for managing tasks requiring human inspection:
    - Lists and inspects pending manual reviews
    - Enforces explicit operator resolutions:
      CONFIRMED_SENT -> marks message SENT, task COMPLETED
      CONFIRMED_NOT_SENT -> marks message FAILED, task FAILED
      RETRY_ALLOWED -> returns task safely to READY state
      CANCELLED -> marks task and message CANCELLED
    - Guarantees tasks are never auto-retried simply by viewing
    """

    def __init__(
        self,
        manual_review_repo: ManualReviewRepository,
        task_repo: TaskRepository,
        message_repo: Optional[MessageRepository] = None,
        event_repo: Optional[EventRepository] = None,
    ):
        self.manual_review_repo = manual_review_repo
        self.task_repo = task_repo
        self.message_repo = message_repo
        self.event_repo = event_repo

    def list_pending(self) -> List[ManualReviewItem]:
        """List all pending items requiring operator attention."""
        return self.manual_review_repo.list_pending()

    def get_item(self, item_id: str) -> Optional[ManualReviewItem]:
        """Retrieve a specific manual review item by ID without mutating state."""
        return self.manual_review_repo.get_by_id(item_id)

    def resolve(
        self,
        item_id: str,
        resolution: str,
        operator_notes: str = "",
    ) -> bool:
        """
        Explicitly resolve a manual review item with one of:
        CONFIRMED_SENT, CONFIRMED_NOT_SENT, RETRY_ALLOWED, CANCELLED.
        """
        resolution_clean = resolution.strip().upper()
        if resolution_clean not in VALID_RESOLUTIONS:
            raise ValueError(f"Invalid manual review resolution: {resolution}. Must be one of {VALID_RESOLUTIONS}")

        item = self.manual_review_repo.get_by_id(item_id)
        if not item or item.status != "PENDING":
            logger.warning(f"Manual review item {item_id} not found or not in PENDING status")
            return False

        task = self.task_repo.get_by_id(item.task_id)
        msg = self.message_repo.get_by_task_id(item.task_id) if self.message_repo else None

        if resolution_clean == "CONFIRMED_SENT":
            if msg and self.message_repo:
                self.message_repo.update_status(msg.id, MessageState.SENT)
            if task:
                self.task_repo.update_state(task.id, TaskState.COMPLETED, enforce_transition=False)

        elif resolution_clean == "CONFIRMED_NOT_SENT":
            if msg and self.message_repo:
                self.message_repo.update_status(msg.id, MessageState.FAILED, result_code="OPERATOR_REJECTED")
            if task:
                self.task_repo.update_state(task.id, TaskState.FAILED, enforce_transition=False)

        elif resolution_clean == "RETRY_ALLOWED":
            if task:
                self.task_repo.update_state(task.id, TaskState.READY, enforce_transition=False)

        elif resolution_clean == "CANCELLED":
            if msg and self.message_repo:
                self.message_repo.update_status(msg.id, MessageState.SKIPPED, result_code="CANCELLED_BY_OPERATOR")
            if task:
                self.task_repo.update_state(task.id, TaskState.CANCELLED, enforce_transition=False)

        # Mark review record resolved
        self.manual_review_repo.resolve(item_id, status=f"RESOLVED_{resolution_clean}")

        if self.event_repo:
            self.event_repo.record(
                event_code=EventCode.MANUAL_REVIEW_RESOLVED,
                category="manual_review",
                level=EventLevel.INFO,
                entity_type="task",
                entity_id=item.task_id,
                payload={
                    "item_id": item_id,
                    "resolution": resolution_clean,
                    "notes": operator_notes,
                },
            )

        logger.info(f"Manual review {item_id} resolved with {resolution_clean}: {operator_notes}")
        return True

    def reject(self, item_id: str, reason: str = "") -> bool:
        """Reject and cancel task under review."""
        return self.resolve(item_id, "CANCELLED", operator_notes=reason)

    def retry_if_safe(self, item_id: str, notes: str = "") -> bool:
        """Allow task retry if confirmed no message was delivered."""
        return self.resolve(item_id, "RETRY_ALLOWED", operator_notes=notes)
