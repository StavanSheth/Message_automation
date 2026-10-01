"""Authoritative execution application service coordinating task execution lifecycle, idempotency, and reconciliation."""

import hashlib
from typing import Optional, Dict, Any, Set
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
        execution_identity_repo: Optional[Any] = None,
    ):
        self.task_repo = task_repo
        self.message_repo = message_repo
        self.automation_service = automation_service
        self.reconciliation_service = reconciliation_service
        self.manual_review_repo = manual_review_repo
        self.event_repo = event_repo
        self._active_execution_keys: Set[str] = set()

        from backend.repositories.execution_identity_repo import ExecutionIdentityRepository
        self.execution_identity_repo = execution_identity_repo or (
            ExecutionIdentityRepository(self.task_repo.db) if hasattr(self.task_repo, "db") else None
        )

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

        actual_task_id = task_id.id if hasattr(task_id, "id") else str(task_id)
        task = self.task_repo.get_by_id(actual_task_id)
        if not task:
            logger.error(f"Cannot execute non-existent task {actual_task_id}")
            return False
        task_id = task.id

        # ── 1. Strict Lease Validation ───────────────────────────
        # A production worker execution path strictly requires a valid held lease
        if not lease_id or not self.task_repo.is_lease_valid(task_id, lease_id, worker_id=worker_id):
            logger.error(f"Execution rejected: no valid active lease held by worker {worker_id} for task {task_id} (lease_id={lease_id})")
            if self.event_repo:
                self.event_repo.record(
                    event_code=EventCode.LEASE_EXPIRED,
                    category="worker",
                    level=EventLevel.WARNING,
                    entity_type="task",
                    entity_id=task_id,
                    task_id=task_id,
                    worker_id=worker_id,
                    correlation_id=corr_id,
                    payload={"worker_id": worker_id, "lease_id": lease_id, "correlation_id": corr_id},
                )
            return False

        # ── 2. Deterministic Execution Identity & Deduplication ───
        exec_key = self._compute_execution_key(task)
        if exec_key in self._active_execution_keys:
            logger.warning(f"Duplicate in-flight execution for key {exec_key}; rejecting")
            return False

        # Persistent DB check
        if self.execution_identity_repo:
            try:
                existing = self.execution_identity_repo.get(exec_key)
                if existing:
                    if existing.state == "SENT":
                        logger.info(f"Execution {exec_key} already confirmed SENT in DB; marking COMPLETED without resending")
                        self.task_repo.update_state(task.id, TaskState.COMPLETED, worker_id=worker_id, enforce_transition=False)
                        return True
                    if existing.state == "RECONCILIATION":
                        logger.warning(f"Execution {exec_key} is in RECONCILIATION in DB; aborting duplicate execution")
                        self.task_repo.update_state(task.id, TaskState.MANUAL_REVIEW, worker_id=worker_id, enforce_transition=False)
                        return False
                    if existing.state == "RUNNING" and existing.worker_id and existing.worker_id != worker_id:
                        logger.warning(f"Execution {exec_key} is currently RUNNING by worker {existing.worker_id}; rejecting concurrent attempt")
                        return False
            except Exception as e:
                logger.debug(f"Could not check execution identity in DB: {e}")

        self._active_execution_keys.add(exec_key)
        if self.execution_identity_repo:
            try:
                from backend.domain.models import ExecutionIdentity
                new_identity = ExecutionIdentity(
                    execution_key=exec_key,
                    task_id=task.id,
                    contact_id=task.contact_id,
                    worker_id=worker_id,
                    session_id=getattr(session, "session_id", None),
                    correlation_id=corr_id,
                    state="RUNNING",
                )
                self.execution_identity_repo.create(new_identity)
            except Exception as e:
                logger.warning(f"Execution identity creation conflict for {exec_key}: {e}; aborting duplicate concurrent execution")
                self._active_execution_keys.discard(exec_key)
                return False

        try:

            # ── 2. Duplicate Send Protection ─────────────────────────
            if self.message_repo.has_confirmed_sent_message(task_id):
                logger.info(f"Task {task_id} already has confirmed SENT message; marking COMPLETED")
                self.task_repo.update_state(task.id, TaskState.COMPLETED, worker_id=worker_id, enforce_transition=False)
                if self.execution_identity_repo:
                    try:
                        self.execution_identity_repo.update_state(exec_key, state="SENT", outcome="CONFIRMED_SENT")
                    except Exception:
                        pass
                return True

            if self.message_repo.is_in_reconciliation(task_id):
                logger.warning(f"Task {task_id} has unresolved message in RECONCILIATION; aborting duplicate execution")
                self.task_repo.update_state(task.id, TaskState.MANUAL_REVIEW, worker_id=worker_id, enforce_transition=False)
                if self.execution_identity_repo:
                    try:
                        self.execution_identity_repo.update_state(exec_key, state="RECONCILIATION", outcome="UNRESOLVED_RECONCILIATION")
                    except Exception:
                        pass
                return False

            # ── 3. Lifecycle Start Event ─────────────────────────────
            if self.event_repo:
                self.event_repo.record(
                    event_code=EventCode.TASK_STARTED,
                    category="execution",
                    level=EventLevel.INFO,
                    entity_type="task",
                    entity_id=task_id,
                    task_id=task_id,
                    worker_id=worker_id,
                    correlation_id=corr_id,
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
                if success:
                    if self.execution_identity_repo:
                        try:
                            self.execution_identity_repo.update_state(exec_key, state="SENT", outcome="CONFIRMED_SENT")
                        except Exception:
                            pass
                else:
                    if self.execution_identity_repo:
                        try:
                            self.execution_identity_repo.update_state(exec_key, state="FAILED", outcome="EXECUTION_FAILED")
                        except Exception:
                            pass
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
                    if self.execution_identity_repo:
                        try:
                            self.execution_identity_repo.update_state(exec_key, state="RECONCILIATION", outcome=f"Exception: {e}")
                        except Exception:
                            pass
                else:
                    self.task_repo.update_state(task.id, TaskState.MANUAL_REVIEW, worker_id=worker_id, enforce_transition=False)
                    if self.execution_identity_repo:
                        try:
                            self.execution_identity_repo.update_state(exec_key, state="FAILED", outcome=f"Exception: {e}")
                        except Exception:
                            pass
                return False
        finally:
            self._active_execution_keys.discard(exec_key)

    @staticmethod
    def _compute_execution_key(task: Task) -> str:
        """Deterministic key for deduplicating concurrent executions of the same logical task."""
        raw = f"{task.contact_id}|{task.id}|{task.type.value}|{task.sequence}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
