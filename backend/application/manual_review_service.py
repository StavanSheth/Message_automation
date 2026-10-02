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
        reconciliation_repo: Optional[Any] = None,
    ):
        self.manual_review_repo = manual_review_repo
        self.task_repo = task_repo
        self.message_repo = message_repo
        self.event_repo = event_repo
        if reconciliation_repo is not None:
            self.reconciliation_repo = reconciliation_repo
        elif hasattr(task_repo, "db"):
            from backend.repositories.reconciliation_repo import ReconciliationRepository
            try:
                self.reconciliation_repo = ReconciliationRepository(task_repo.db)
            except Exception:
                self.reconciliation_repo = None
        else:
            self.reconciliation_repo = None

    def list_pending(self) -> List[ManualReviewItem]:
        """List all pending items requiring operator attention."""
        return self.manual_review_repo.list_pending()

    def get(self, item_id: str) -> Optional[ManualReviewItem]:
        """Alias for get_item."""
        return self.get_item(item_id)

    def get_item(self, item_id: str) -> Optional[ManualReviewItem]:
        """Retrieve a specific manual review item by ID without mutating state."""
        return self.manual_review_repo.get_by_id(item_id)

    def resolve(
        self,
        item_id: str,
        resolution: str,
        operator_notes: str = "",
        operator: str = "operator",
        evidence: str = "",
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
                # Reset task safely to READY for normal scheduling. Does NOT direct send.
                self.task_repo.update_state(task.id, TaskState.READY, enforce_transition=False)

        elif resolution_clean == "CANCELLED":
            if msg and self.message_repo:
                self.message_repo.update_status(msg.id, MessageState.SKIPPED, result_code="CANCELLED_BY_OPERATOR")
            if task:
                self.task_repo.update_state(task.id, TaskState.CANCELLED, enforce_transition=False)

        # Mark review record resolved with explicit metadata
        self.manual_review_repo.resolve(
            item_id,
            status="RESOLVED",
            resolution=resolution_clean,
            resolved_by=operator,
            resolution_notes=operator_notes or evidence,
        )

        # Also resolve corresponding reconciliation record if exists
        if self.reconciliation_repo:
            try:
                rec = self.reconciliation_repo.get_by_task_id(item.task_id)
                if rec and rec.state == "PENDING":
                    self.reconciliation_repo.update_resolution(
                        record_id=rec.id,
                        state="RESOLVED",
                        resolution=resolution_clean,
                        resolution_source=f"MANUAL_REVIEW:{operator}",
                        observed_state=operator_notes or evidence,
                    )
            except Exception as e:
                logger.warning(f"Could not resolve reconciliation record for task {item.task_id}: {e}")

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
                    "operator": operator,
                    "notes": operator_notes,
                    "evidence": evidence,
                },
            )

        logger.info(f"Manual review {item_id} resolved with {resolution_clean} by {operator}: {operator_notes}")
        return True

    def confirm_sent(self, item_id: str, operator: str = "operator", notes: str = "", evidence: str = "") -> bool:
        """Confirm message was sent; marks task COMPLETED."""
        return self.resolve(item_id, "CONFIRMED_SENT", operator_notes=notes, operator=operator, evidence=evidence)

    def confirm_not_sent(self, item_id: str, operator: str = "operator", notes: str = "", evidence: str = "") -> bool:
        """Confirm message was not sent; marks task FAILED."""
        return self.resolve(item_id, "CONFIRMED_NOT_SENT", operator_notes=notes, operator=operator, evidence=evidence)

    def allow_retry(self, item_id: str, operator: str = "operator", notes: str = "", evidence: str = "") -> bool:
        """Safely allow task to be re-evaluated by scheduler; resets state to READY."""
        return self.resolve(item_id, "RETRY_ALLOWED", operator_notes=notes, operator=operator, evidence=evidence)

    def cancel(self, item_id: str, operator: str = "operator", notes: str = "", evidence: str = "") -> bool:
        """Cancel task and message."""
        return self.resolve(item_id, "CANCELLED", operator_notes=notes, operator=operator, evidence=evidence)

    def reject(self, item_id: str, reason: str = "") -> bool:
        """Reject and cancel task under review."""
        return self.cancel(item_id, notes=reason)

    def retry_if_safe(self, item_id: str, notes: str = "") -> bool:
        """Allow task retry if confirmed no message was delivered."""
        return self.allow_retry(item_id, notes=notes)

