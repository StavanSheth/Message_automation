"""Centralized reconciliation service orchestrating ambiguous send recovery."""

from typing import Optional, List, Dict, Any
from backend.domain.models import ReconciliationRecord, ManualReviewItem, Task, Message, utc_now_iso
from backend.domain.enums import (
    TaskState,
    MessageState,
    EventCode,
    EventLevel,
    ReconciliationState,
    ReconciliationResolution,
)
from backend.reconciliation.models import ReconciliationContext, ReconciliationResult
from backend.reconciliation.resolver import ReconciliationResolver
from backend.repositories.task_repo import TaskRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.reconciliation_repo import ReconciliationRepository
from backend.repositories.manual_review_repo import ManualReviewRepository
from backend.repositories.event_repo import EventRepository
from backend.events.correlation import generate_id
from backend.events.logger import get_logger

logger = get_logger("reconciliation_service")


class ReconciliationService:
    """
    Centralized reconciliation engine managing the end-to-end reconciliation lifecycle.
    Guarantees that an ambiguous send never results in an automatic re-send.
    """

    def __init__(
        self,
        reconciliation_repo: ReconciliationRepository,
        task_repo: TaskRepository,
        message_repo: MessageRepository,
        manual_review_repo: Optional[ManualReviewRepository] = None,
        event_repo: Optional[EventRepository] = None,
        resolver: Optional[ReconciliationResolver] = None,
    ):
        self.reconciliation_repo = reconciliation_repo
        self.task_repo = task_repo
        self.message_repo = message_repo
        self.manual_review_repo = manual_review_repo
        self.event_repo = event_repo
        self.resolver = resolver or ReconciliationResolver()

    def enter_reconciliation(
        self,
        task_id: str,
        reason: str,
        message_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        session_id: Optional[str] = None,
        observed_state: Optional[str] = None,
    ) -> ReconciliationRecord:
        """
        Move a task and message into RECONCILING / RECONCILIATION status and create a record.
        """
        # 0. Idempotency guard: return existing pending reconciliation record
        try:
            existing = self.reconciliation_repo.get_by_task_id(task_id)
            if existing and existing.state == ReconciliationState.PENDING.value:
                return existing
        except Exception:
            pass

        # 1. Update task state
        try:
            self.task_repo.update_state(task_id, TaskState.RECONCILING, worker_id=worker_id, enforce_transition=False)
        except Exception as e:
            logger.warning(f"Could not update task {task_id} to RECONCILING: {e}")

        # 2. Update message state
        if not message_id:
            try:
                m = self.message_repo.get_by_task_id(task_id)
                if m:
                    message_id = m.id
            except Exception:
                pass

        if message_id:
            try:
                self.message_repo.update_status(message_id, MessageState.RECONCILIATION, result_code="AMBIGUOUS")
            except Exception as e:
                logger.warning(f"Could not update message {message_id} to RECONCILIATION: {e}")

        # 3. Create persistent reconciliation record
        record = ReconciliationRecord(
            id=generate_id("REC"),
            task_id=task_id,
            message_id=message_id,
            worker_id=worker_id,
            session_id=session_id,
            state=ReconciliationState.PENDING.value,
            reason=reason,
            observed_state=observed_state,
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
        )
        created = self.reconciliation_repo.create(record)

        if self.event_repo:
            self.event_repo.record(
                event_code=EventCode.RECONCILIATION_STARTED,
                category="reconciliation",
                level=EventLevel.WARNING,
                entity_type="task",
                entity_id=task_id,
                payload={
                    "reconciliation_id": created.id,
                    "reason": reason,
                    "worker_id": worker_id,
                    "message_id": message_id,
                },
            )

        logger.warning(f"Task {task_id} entered reconciliation: {reason}")
        return created

    def resolve_reconciliation(
        self,
        reconciliation_id: str,
        verification_confirmed: Optional[bool] = None,
        composer_empty: Optional[bool] = None,
        has_error_banner: bool = False,
        source: str = "AUTO_RESOLVER",
    ) -> ReconciliationResult:
        """
        Evaluate and resolve a pending reconciliation record.
        Updates task, message, and reconciliation records.
        If resolution is MANUAL_REVIEW, enqueues item into manual review queue.
        """
        rec = self.reconciliation_repo.get_by_id(reconciliation_id)
        if not rec:
            rec = self.reconciliation_repo.get_by_task_id(reconciliation_id)
        if not rec:
            raise ValueError(f"Reconciliation record '{reconciliation_id}' not found")

        context = ReconciliationContext(
            task_id=rec.task_id,
            message_id=rec.message_id,
            worker_id=rec.worker_id,
            session_id=rec.session_id,
            reason=rec.reason,
            observed_state=rec.observed_state,
        )

        result = self.resolver.resolve(
            context=context,
            verification_confirmed=verification_confirmed,
            composer_empty=composer_empty,
            has_error_banner=has_error_banner,
            source=source,
        )

        now_iso = utc_now_iso()

        # Update reconciliation repository
        self.reconciliation_repo.update_resolution(
            record_id=rec.id,
            state=ReconciliationState.RESOLVED.value,
            resolution=result.resolution.value,
            resolution_source=source,
            observed_state=result.details,
        )

        # Apply target task state
        target_task_state = TaskState(result.target_task_state)
        self.task_repo.update_state(rec.task_id, target_task_state, enforce_transition=False)

        # Apply target message state
        if rec.message_id:
            target_msg_state = MessageState(result.target_message_state)
            confirmed_at = now_iso if target_msg_state == MessageState.SENT else None
            self.message_repo.update_status(
                rec.message_id,
                target_msg_state,
                result_code=result.resolution.value,
                confirmed_at=confirmed_at,
            )

        # Escalate to manual review queue if needed
        if result.resolution == ReconciliationResolution.MANUAL_REVIEW and self.manual_review_repo:
            task = self.task_repo.get_by_id(rec.task_id)
            contact_id = task.contact_id if task else "UNKNOWN"
            review_item = ManualReviewItem(
                id=generate_id("REV"),
                task_id=rec.task_id,
                contact_id=contact_id,
                reason=f"Reconciliation unresolved: {rec.reason}",
                current_state=target_task_state.value,
                evidence_json=result.details,
                recommended_action="Inspect Instagram DM thread manually to verify delivery before resuming",
                status="PENDING",
            )
            self.manual_review_repo.create(review_item)

        if self.event_repo:
            self.event_repo.record(
                event_code=EventCode.RECONCILIATION_RESOLVED,
                category="reconciliation",
                level=EventLevel.INFO,
                entity_type="task",
                entity_id=rec.task_id,
                payload={
                    "reconciliation_id": rec.id,
                    "resolution": result.resolution.value,
                    "details": result.details,
                },
            )

        return result
