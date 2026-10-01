"""Authoritative execution application service coordinating task execution lifecycle, idempotency, and reconciliation."""

from typing import Optional, Dict, Any
from backend.domain.models import Task, utc_now_iso
from backend.domain.enums import TaskState, MessageState, EventCode, EventLevel, ErrorCode
from backend.repositories.task_repo import TaskRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.manual_review_repo import ManualReviewRepository
from backend.reconciliation.service import ReconciliationService
from backend.browser.session import BrowserSessionInstance
from backend.browser.instagram.automation_service import InstagramAutomationService
from backend.events.correlation import generate_id
from backend.events.logger import get_logger

logger = get_logger("execution_service")


class ExecutionService:
    """
    Production-grade execution coordinator managing:
    - Complete message execution lifecycle: CREATED -> VALIDATING -> READY -> RUNNING -> SENDING -> VERIFYING -> SENT
    - Deterministic execution identity and duplicate-send protection.
    - Lease validation ensuring only current lease holder executes.
    - Delegation to InstagramAutomationService with single send action.
    - Fail-safe reconciliation routing for ambiguous send/verification outcomes.
    - Manual review escalation for blocked or unrecognized states.
    - Full event logging and correlation tracking.
    """

    def __init__(
        self,
        task_repo: TaskRepository,
        message_repo: MessageRepository,
        automation_service: InstagramAutomationService,
        reconciliation_service: Optional[ReconciliationService] = None,
        manual_review_repo: Optional[ManualReviewRepository] = None,
        event_repo: Optional[EventRepository] = None,
    ):
        self.task_repo = task_repo
        self.message_repo = message_repo
        self.automation_service = automation_service
        self.reconciliation_service = reconciliation_service
        self.manual_review_repo = manual_review_repo
        self.event_repo = event_repo

    def execute_task(
        self,
        task_id: str,
        session: BrowserSessionInstance,
        worker_id: str,
        lease_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> bool:
        """
        Authoritatively execute a messaging task with complete safety guarantees.
        """
        corr_id = correlation_id or generate_id("CORR")
        now_iso = utc_now_iso()

        task = self.task_repo.get_by_id(task_id)
        if not task:
            logger.error(f"Cannot execute non-existent task {task_id}")
            return False

        # ── 1. Lease Validation ──────────────────────────────────
        if lease_id and not self.task_repo.is_lease_valid(task_id, lease_id):
            logger.error(f"Lease {lease_id} is expired or invalid for task {task_id}; aborting execution")
            if self.event_repo:
                self.event_repo.record(
                    event_code=EventCode.LEASE_EXPIRED,
                    category="worker",
                    level=EventLevel.WARNING,
                    entity_type="task",
                    entity_id=task_id,
                    payload={"worker_id": worker_id, "lease_id": lease_id, "correlation_id": corr_id},
                )
            return False

        # ── 2. Duplicate Send Protection ─────────────────────────
        if self.message_repo.has_confirmed_sent_message(task_id):
            logger.info(f"Task {task_id} already has confirmed SENT message; marking COMPLETED")
            self.task_repo.update_state(task.id, TaskState.COMPLETED, worker_id=worker_id, enforce_transition=False)
            return True

        if self.message_repo.is_in_reconciliation(task_id):
            logger.warning(f"Task {task_id} has unresolved message in RECONCILIATION; aborting duplicate execution")
            self.task_repo.update_state(task.id, TaskState.MANUAL_REVIEW, worker_id=worker_id, enforce_transition=False)
            return False

        # ── 3. Lifecycle Start Event ─────────────────────────────
        if self.event_repo:
            self.event_repo.record(
                event_code=EventCode.TASK_STARTED,
                category="execution",
                level=EventLevel.INFO,
                entity_type="task",
                entity_id=task_id,
                payload={"worker_id": worker_id, "correlation_id": corr_id},
            )

        # ── 4. Execute via InstagramAutomationService ────────────
        try:
            success = self.automation_service.execute_messaging_task(
                task=task,
                session=session,
                worker_id=worker_id,
                correlation_id=corr_id,
            )
            return success
        except Exception as e:
            logger.error(f"Unexpected exception during execution of task {task_id}: {e}", exc_info=True)
            # Route to reconciliation or manual review
            if self.reconciliation_service:
                self.reconciliation_service.enter_reconciliation(
                    task_id=task.id,
                    worker_id=worker_id,
                    session_id=getattr(session, "session_id", None),
                    reason=f"Execution exception: {e}",
                )
            else:
                self.task_repo.update_state(task.id, TaskState.MANUAL_REVIEW, worker_id=worker_id, enforce_transition=False)
            return False
