"""Instagram Automation Service orchestrating the complete Instagram messaging workflow."""

from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any

from backend.domain.models import Task, Contact, Message, ErrorRecord, utc_now_iso
from backend.domain.enums import (
    TaskState,
    TaskType,
    MessageState,
    FollowupStatus,
    ErrorCode,
    ErrorSeverity,
    EventCode,
    EventLevel,
    VerificationDecision,
    SessionAuthState,
)
from backend.domain.errors import AutomationError
from backend.browser.session import BrowserSessionInstance
from backend.browser.exceptions import BrowserCrashError, BrowserTimeoutError
from backend.browser.instagram.navigator import InstagramNavigator, InstagramPageStatus
from backend.browser.instagram.profile_reader import InstagramProfileReader
from backend.browser.instagram.profile_verifier import InstagramProfileVerifier
from backend.browser.instagram.message_composer import InstagramMessageComposer
from backend.browser.instagram.message_sender import InstagramMessageSender
from backend.browser.instagram.send_verifier import InstagramSendVerifier
from backend.browser.instagram.auth_validator import InstagramAuthValidator
from backend.repositories.task_repo import TaskRepository
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.followup_repo import FollowupRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.error_repo import ErrorRepository
from backend.repositories.verification_result_repo import VerificationResultRepository
from backend.config.settings import AppSettings, get_settings
from backend.events.correlation import generate_id
from backend.events.logger import get_logger

logger = get_logger("instagram_automation_service")


class InstagramAutomationService:
    """
    Coordinates profile navigation, profile verification, message composition,
    submission, post-send verification, state persistence, error recording, and follow-up scheduling.
    """

    def __init__(
        self,
        task_repo: TaskRepository,
        contact_repo: ContactRepository,
        message_repo: MessageRepository,
        followup_repo: FollowupRepository,
        event_repo: EventRepository,
        error_repo: ErrorRepository,
        verification_repo: Optional[VerificationResultRepository] = None,
        settings: Optional[AppSettings] = None,
        navigator: Optional[InstagramNavigator] = None,
        reader: Optional[InstagramProfileReader] = None,
        verifier: Optional[InstagramProfileVerifier] = None,
        composer: Optional[InstagramMessageComposer] = None,
        sender: Optional[InstagramMessageSender] = None,
        send_verifier: Optional[InstagramSendVerifier] = None,
        auth_validator: Optional[InstagramAuthValidator] = None,
        reconciliation_service: Optional[Any] = None,
        followup_service: Optional[Any] = None,
        cooldown_repo: Optional[Any] = None,
    ):
        self.task_repo = task_repo
        self.contact_repo = contact_repo
        self.message_repo = message_repo
        self.followup_repo = followup_repo
        self.event_repo = event_repo
        self.error_repo = error_repo
        self.verification_repo = verification_repo
        self.settings = settings or get_settings()

        from backend.repositories.cooldown_repo import CooldownRepository
        self.cooldown_repo = cooldown_repo or (
            CooldownRepository(self.task_repo.db) if hasattr(self.task_repo, "db") else None
        )

        self.navigator = navigator or InstagramNavigator()
        self.reader = reader or InstagramProfileReader()
        self.verifier = verifier or InstagramProfileVerifier(
            verification_repo=self.verification_repo,
            threshold=self.settings.verification_threshold,
        )
        self.composer = composer or InstagramMessageComposer()
        self.sender = sender or InstagramMessageSender()
        self.send_verifier = send_verifier or InstagramSendVerifier()
        self.auth_validator = auth_validator or InstagramAuthValidator()
        self.reconciliation_service = reconciliation_service
        self.followup_service = followup_service
        self._rate_limit_cooldown_until: Optional[datetime] = None
        self._rate_limit_cooldown_seconds: int = getattr(
            self.settings, "rate_limit_cooldown_seconds", getattr(self.settings, "rate_limit_cooldown", 900)
        )

    def _activate_cooldown(
        self,
        reason: str,
        error_code: ErrorCode,
        worker_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> None:
        """Activate both in-memory and persistent cross-process rate-limit cooldown."""
        duration = self._rate_limit_cooldown_seconds
        expires_dt = datetime.now(timezone.utc) + timedelta(seconds=duration)
        self._rate_limit_cooldown_until = expires_dt
        logger.warning(f"Rate-limit cooldown activated for {duration}s: {reason}")

        if self.cooldown_repo:
            try:
                from backend.domain.models import RateLimitCooldown
                cool_rec = RateLimitCooldown(
                    id=generate_id("COOL"),
                    scope="GLOBAL",
                    reason=reason,
                    error_code=error_code.value,
                    detected_at=utc_now_iso(),
                    cooldown_until=expires_dt.isoformat(),
                    detected_by_worker=worker_id,
                    detected_by_session=session_id,
                )
                self.cooldown_repo.record_cooldown(cool_rec)
            except Exception as e:
                logger.debug(f"Could not record persistent cooldown: {e}")

    def execute_messaging_task(
        self,
        task: Task,
        session: BrowserSessionInstance,
        worker_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> bool:
        """
        Execute an initial message or follow-up messaging task.
        State progression:
        READY -> VALIDATING -> RUNNING -> SENDING -> VERIFYING -> COMPLETED
        """
        corr_id = correlation_id or generate_id("CORR")
        now_iso = utc_now_iso()

        # ── 1. Validate Task and Contact ──────────────────────────
        current_task = self.task_repo.get_by_id(task.id)
        if not current_task:
            logger.error(f"Task {task.id} not found")
            return False

        contact = self.contact_repo.get_by_id(current_task.contact_id)
        if not contact:
            self._record_error(current_task, ErrorCode.INVALID_DATA, "Contact not found", retryable=False, worker_id=worker_id)
            self.task_repo.update_state(current_task.id, TaskState.FAILED, worker_id=worker_id)
            return False

        # Guard: Check replied state
        if contact.replied_status.value == "YES":
            logger.info(f"Contact {contact.id} already replied; skipping task {task.id}")
            self.followup_repo.cancel_pending_for_contact(contact.id, cancel_reason="REPLIED")
            self.task_repo.update_state(current_task.id, TaskState.SKIPPED, worker_id=worker_id)
            return True

        # Guard: Deduplication check - never send if task already has confirmed SENT message
        if self.message_repo.has_confirmed_sent_message(current_task.id):
            logger.info(f"Task {current_task.id} already has a confirmed SENT message; marking COMPLETED without sending")
            self.task_repo.update_state(current_task.id, TaskState.COMPLETED, worker_id=worker_id)
            return True

        # Guard: Do not send if previous attempt is in RECONCILIATION
        if self.message_repo.is_in_reconciliation(current_task.id):
            logger.warning(f"Task {current_task.id} has a message in RECONCILIATION; refusing to send again")
            self.task_repo.update_state(current_task.id, TaskState.MANUAL_REVIEW, worker_id=worker_id)
            return False

        # Fetch message record to send
        message_record = self._get_or_create_message_record(current_task, contact)
        if not message_record:
            self._record_error(current_task, ErrorCode.INVALID_DATA, "No message body found for task", retryable=False, worker_id=worker_id)
            self.task_repo.update_state(current_task.id, TaskState.FAILED, worker_id=worker_id)
            return False

        # ── 2. Transition Task to VALIDATING ─────────────────────
        try:
            self.task_repo.update_state(current_task.id, TaskState.VALIDATING, worker_id=worker_id)
        except Exception as e:
            logger.warning(f"Could not transition task to VALIDATING: {e}")

        # ── 2a. Rate-Limit Cooldown Check ────────────────────────
        active_cooldown_reason = None
        if self._rate_limit_cooldown_until and datetime.now(timezone.utc) < self._rate_limit_cooldown_until:
            rem = (self._rate_limit_cooldown_until - datetime.now(timezone.utc)).total_seconds()
            active_cooldown_reason = f"Rate-limit cooldown active ({rem:.0f}s remaining)"
        elif self.cooldown_repo:
            try:
                db_cool = self.cooldown_repo.get_active_cooldown(scope="GLOBAL")
                if db_cool:
                    active_cooldown_reason = f"Persistent {db_cool.scope} cooldown active: {db_cool.reason}"
            except Exception as e:
                logger.debug(f"Could not check persistent cooldown: {e}")

        if active_cooldown_reason:
            logger.warning(f"Rate-limit cooldown active; deferring task {current_task.id}: {active_cooldown_reason}")
            self._record_error(current_task, ErrorCode.RATE_LIMITED, active_cooldown_reason, retryable=True, worker_id=worker_id)
            self.task_repo.update_state(current_task.id, TaskState.RETRY_WAIT, worker_id=worker_id, enforce_transition=False)
            return False

        # ── 2b. Session Validation ──────────────────────────────
        if not session or not session.is_alive():
            self._record_error(current_task, ErrorCode.BROWSER_CRASH, "Browser session not alive", retryable=True, worker_id=worker_id)
            self.task_repo.update_state(current_task.id, TaskState.RETRY_WAIT, worker_id=worker_id)
            return False

        # ── 2c. Authentication Validation (Fail-Closed) ──────────
        try:
            from backend.domain.enums import SessionAuthState
            auth_state, auth_reason = self.auth_validator.check_auth_state(session)
            if auth_state == SessionAuthState.LOGIN_REQUIRED:
                self._record_error(current_task, ErrorCode.SESSION_EXPIRED, f"Session requires login: {auth_reason}", retryable=False, worker_id=worker_id)
                self.event_repo.record(
                    event_code=EventCode.LOGIN_REQUIRED,
                    category="worker",
                    level=EventLevel.CRITICAL,
                    entity_type="task",
                    entity_id=current_task.id,
                    task_id=current_task.id,
                    worker_id=worker_id,
                    correlation_id=corr_id,
                    payload={"worker_id": worker_id, "auth_reason": auth_reason, "correlation_id": corr_id},
                )
                self.task_repo.update_state(current_task.id, TaskState.MANUAL_REVIEW, worker_id=worker_id, enforce_transition=False)
                return False

            if auth_state in (SessionAuthState.CHALLENGE, SessionAuthState.CHECKPOINT):
                self._record_error(current_task, ErrorCode.CHALLENGE_REQUIRED, f"Instagram challenge/checkpoint: {auth_reason}", retryable=False, worker_id=worker_id)
                self.event_repo.record(
                    event_code=EventCode.LOGIN_REQUIRED,
                    category="worker",
                    level=EventLevel.CRITICAL,
                    entity_type="task",
                    entity_id=current_task.id,
                    task_id=current_task.id,
                    worker_id=worker_id,
                    correlation_id=corr_id,
                    payload={"worker_id": worker_id, "auth_reason": auth_reason, "auth_state": auth_state.value, "correlation_id": corr_id},
                )
                self.task_repo.update_state(current_task.id, TaskState.MANUAL_REVIEW, worker_id=worker_id, enforce_transition=False)
                return False

            if auth_state == SessionAuthState.SESSION_EXPIRED:
                self._record_error(current_task, ErrorCode.SESSION_EXPIRED, f"Session expired: {auth_reason}", retryable=False, worker_id=worker_id)
                self.task_repo.update_state(current_task.id, TaskState.MANUAL_REVIEW, worker_id=worker_id, enforce_transition=False)
                return False

            if auth_state != SessionAuthState.AUTHENTICATED:
                # Any non-authenticated state (including UNKNOWN) must FAIL-CLOSED
                self._record_error(current_task, ErrorCode.SESSION_EXPIRED, f"Instagram authentication unconfirmed ({auth_state.value}): {auth_reason}", retryable=False, worker_id=worker_id)
                self.task_repo.update_state(current_task.id, TaskState.MANUAL_REVIEW, worker_id=worker_id, enforce_transition=False)
                return False

        except Exception as e:
            logger.error(f"Auth validation exception (failing closed to MANUAL_REVIEW): {e}", exc_info=True)
            self._record_error(current_task, ErrorCode.SESSION_EXPIRED, f"Auth validation failure: {e}", retryable=False, worker_id=worker_id)
            self.task_repo.update_state(current_task.id, TaskState.MANUAL_REVIEW, worker_id=worker_id, enforce_transition=False)
            return False

        # ── 3. Profile Navigation ────────────────────────────────
        nav_result = self.navigator.navigate_to_profile(session, contact.instagram_url)
        page_status = nav_result.get("status", InstagramPageStatus.AVAILABLE)

        self.event_repo.record(
            event_code=EventCode.PROFILE_OPENED,
            category="worker",
            level=EventLevel.INFO,
            entity_type="task",
            entity_id=current_task.id,
            task_id=current_task.id,
            worker_id=worker_id,
            correlation_id=corr_id,
            payload={"url": contact.instagram_url, "status": page_status, "correlation_id": corr_id},
        )

        if page_status == InstagramPageStatus.LOGIN_REQUIRED:
            self._record_error(
                current_task, ErrorCode.SESSION_EXPIRED, "Instagram login required", retryable=False, worker_id=worker_id
            )
            self.event_repo.record(
                event_code=EventCode.LOGIN_REQUIRED,
                category="worker",
                level=EventLevel.CRITICAL,
                entity_type="task",
                entity_id=current_task.id,
                task_id=current_task.id,
                worker_id=worker_id,
                correlation_id=corr_id,
                payload={"worker_id": worker_id, "url": nav_result.get("url"), "correlation_id": corr_id},
            )
            self.task_repo.update_state(current_task.id, TaskState.MANUAL_REVIEW, worker_id=worker_id)
            return False

        if page_status == InstagramPageStatus.NOT_FOUND:
            self._record_error(
                current_task, ErrorCode.PROFILE_NOT_FOUND, "Target profile not found on Instagram", retryable=False, worker_id=worker_id
            )
            self.task_repo.update_state(current_task.id, TaskState.SKIPPED, worker_id=worker_id)
            return False

        if page_status == InstagramPageStatus.ACCESS_BLOCKED:
            self._activate_cooldown(
                reason="Instagram profile navigation ACCESS_BLOCKED",
                error_code=ErrorCode.ACTION_BLOCKED,
                worker_id=worker_id,
                session_id=getattr(session, "session_id", None),
            )
            self._record_error(
                current_task, ErrorCode.ACTION_BLOCKED, "Instagram access blocked or rate limited", retryable=False, worker_id=worker_id
            )
            self.task_repo.update_state(current_task.id, TaskState.MANUAL_REVIEW, worker_id=worker_id)
            return False

        if page_status == InstagramPageStatus.UNKNOWN:
            self._record_error(
                current_task, ErrorCode.UI_CHANGED, "Instagram UI structure unrecognized", retryable=False, worker_id=worker_id
            )
            self.event_repo.record(
                event_code=EventCode.MANUAL_REVIEW_REQUIRED,
                category="worker",
                level=EventLevel.WARNING,
                entity_type="task",
                entity_id=current_task.id,
                payload={"worker_id": worker_id, "reason": "unrecognized_profile_ui", "correlation_id": corr_id},
            )
            self.task_repo.update_state(current_task.id, TaskState.MANUAL_REVIEW, worker_id=worker_id)
            return False

        if page_status == InstagramPageStatus.RESTRICTED:
            self._record_error(
                current_task, ErrorCode.ACCESS_PROHIBITED, "Target profile is restricted/unavailable", retryable=False, worker_id=worker_id
            )
            self.task_repo.update_state(current_task.id, TaskState.MANUAL_REVIEW, worker_id=worker_id)
            return False

        if page_status == InstagramPageStatus.UNAVAILABLE:
            self._record_error(
                current_task, ErrorCode.TIMEOUT, "Navigation to profile timed out or failed", retryable=True, worker_id=worker_id
            )
            self.task_repo.update_state(current_task.id, TaskState.RETRY_WAIT, worker_id=worker_id)
            return False

        # ── 4. Profile Extraction & Verification ──────────────────
        observed = self.reader.extract_profile(session)
        decision, confidence, signals, _ = self.verifier.verify_profile(
            contact=contact,
            observed=observed,
            task_id=current_task.id,
        )

        self.event_repo.record(
            event_code=EventCode.PROFILE_VERIFIED,
            category="worker",
            level=EventLevel.INFO,
            entity_type="task",
            entity_id=current_task.id,
            task_id=current_task.id,
            worker_id=worker_id,
            correlation_id=corr_id,
            payload={"decision": decision.value, "confidence": confidence, "correlation_id": corr_id},
        )

        if decision == VerificationDecision.MISMATCH:
            self._record_error(
                current_task, ErrorCode.PROFILE_MISMATCH, "Profile identity mismatch detected", retryable=False, worker_id=worker_id
            )
            self.task_repo.update_state(current_task.id, TaskState.MANUAL_REVIEW, worker_id=worker_id)
            return False

        if decision == VerificationDecision.NOT_FOUND:
            self._record_error(
                current_task, ErrorCode.PROFILE_NOT_FOUND, "Profile details missing or not found", retryable=False, worker_id=worker_id
            )
            self.task_repo.update_state(current_task.id, TaskState.SKIPPED, worker_id=worker_id)
            return False

        send_allowed = self.verifier.is_send_allowed(decision, execution_mode=self.settings.execution_mode)
        if not send_allowed:
            logger.warning(f"Verification decision '{decision.value}' requires manual review for task {task.id}")
            self.message_repo.update_status(message_record.id, MessageState.AWAITING_APPROVAL)
            self.task_repo.update_state(current_task.id, TaskState.MANUAL_REVIEW, worker_id=worker_id)
            return False

        # ── 5. Check DM Availability ─────────────────────────────
        if not observed.get("can_message", False):
            logger.warning(f"Direct messaging unavailable for contact {contact.id}")
            self._record_error(
                current_task, ErrorCode.DM_NOT_AVAILABLE, "DM action not available on profile", retryable=False, worker_id=worker_id
            )
            self.message_repo.update_status(message_record.id, MessageState.SKIPPED)
            self.task_repo.update_state(current_task.id, TaskState.SKIPPED, worker_id=worker_id)
            return False

        # ── 6. Open Dialog & Compose Message ──────────────────────
        self.task_repo.update_state(current_task.id, TaskState.RUNNING, worker_id=worker_id, enforce_transition=False)

        dialog_res = self.composer.open_message_dialog(session)
        if not dialog_res.get("success"):
            self._record_error(
                current_task, ErrorCode.DM_NOT_AVAILABLE, "Failed to open message dialog", retryable=True, worker_id=worker_id
            )
            self.message_repo.update_status(message_record.id, MessageState.FAILED)
            self.task_repo.update_state(current_task.id, TaskState.RETRY_WAIT, worker_id=worker_id)
            return False

        compose_res = self.composer.compose_message(session, message_record.body)
        if not compose_res.get("success"):
            self._record_error(
                current_task, ErrorCode.MESSAGE_SEND_FAILED, "Failed to enter message content", retryable=True, worker_id=worker_id
            )
            self.message_repo.update_status(message_record.id, MessageState.FAILED)
            self.task_repo.update_state(current_task.id, TaskState.RETRY_WAIT, worker_id=worker_id)
            return False

        self.event_repo.record(
            event_code=EventCode.MESSAGE_COMPOSED,
            category="messaging",
            level=EventLevel.INFO,
            entity_type="message",
            entity_id=message_record.id,
            task_id=current_task.id,
            worker_id=worker_id,
            correlation_id=corr_id,
            payload={"task_id": current_task.id, "correlation_id": corr_id},
        )

        # ── 7. Submit / Send Message (Strict Single Send) ──────────
        self.message_repo.update_status(message_record.id, MessageState.SENDING)

        self.event_repo.record(
            event_code=EventCode.MESSAGE_SEND_STARTED,
            category="messaging",
            level=EventLevel.INFO,
            entity_type="message",
            entity_id=message_record.id,
            task_id=current_task.id,
            worker_id=worker_id,
            correlation_id=corr_id,
            payload={"task_id": current_task.id, "contact_id": contact.id, "correlation_id": corr_id},
        )

        send_res = self.sender.submit_send(session)
        if not send_res.get("submitted"):
            err_code = send_res.get("error_code") or ErrorCode.MESSAGE_SEND_FAILED
            is_ambiguous = send_res.get("is_ambiguous", False) or err_code == ErrorCode.UNKNOWN_RESULT
            if is_ambiguous and self.reconciliation_service:
                self.task_repo.update_state(current_task.id, TaskState.RECONCILING, worker_id=worker_id, enforce_transition=False)
                self.message_repo.update_status(message_record.id, MessageState.RECONCILIATION)
                self.event_repo.record(
                    event_code=EventCode.MESSAGE_SEND_AMBIGUOUS,
                    category="messaging",
                    level=EventLevel.WARNING,
                    entity_type="message",
                    entity_id=message_record.id,
                    task_id=current_task.id,
                    worker_id=worker_id,
                    correlation_id=corr_id,
                    payload={"task_id": current_task.id, "reason": send_res.get("reason"), "correlation_id": corr_id},
                )
                self.reconciliation_service.enter_reconciliation(
                    task_id=current_task.id,
                    message_id=message_record.id,
                    worker_id=worker_id,
                    session_id=session.session_id if session else None,
                    reason=send_res.get("reason", "send_ambiguous"),
                )
                return False
            else:
                # Activate rate-limit cooldown if send was blocked
                if err_code in (ErrorCode.ACTION_BLOCKED, ErrorCode.ACCESS_PROHIBITED, ErrorCode.RATE_LIMITED):
                    self._activate_cooldown(
                        reason=f"Send action blocked: {send_res.get('reason', err_code.value)}",
                        error_code=err_code,
                        worker_id=worker_id,
                        session_id=getattr(session, "session_id", None),
                    )
                self._record_error(
                    current_task, err_code, send_res.get("reason", "Send failed"), retryable=False, worker_id=worker_id
                )
                self.message_repo.update_status(message_record.id, MessageState.FAILED)
                self.task_repo.update_state(current_task.id, TaskState.FAILED, worker_id=worker_id)
                return False

        # ── 8. Send Verification ──────────────────────────────────
        self.message_repo.update_status(message_record.id, MessageState.VERIFYING)

        self.event_repo.record(
            event_code=EventCode.MESSAGE_VERIFICATION_STARTED,
            category="messaging",
            level=EventLevel.INFO,
            entity_type="message",
            entity_id=message_record.id,
            task_id=current_task.id,
            worker_id=worker_id,
            correlation_id=corr_id,
            payload={"task_id": current_task.id, "correlation_id": corr_id},
        )

        verification_res = self.send_verifier.verify_sent_message(session, message_record.body, contact.username)

        if verification_res.get("confirmed"):
            now_sent = utc_now_iso()
            self.message_repo.update_status(
                message_record.id,
                MessageState.SENT,
                confirmed_at=now_sent,
                result_code="SUCCESS",
            )
            self.task_repo.update_state(current_task.id, TaskState.COMPLETED, worker_id=worker_id)

            self.event_repo.record(
                event_code=EventCode.MESSAGE_VERIFIED,
                category="messaging",
                level=EventLevel.INFO,
                entity_type="message",
                entity_id=message_record.id,
                task_id=current_task.id,
                worker_id=worker_id,
                correlation_id=corr_id,
                payload={"task_id": current_task.id, "contact_id": contact.id, "confirmed_at": now_sent, "correlation_id": corr_id},
            )

            # Schedule follow-up after prerequisite message confirms
            self._schedule_next_followup(current_task, contact, now_sent)
            return True
        else:
            self.task_repo.update_state(current_task.id, TaskState.RECONCILING, worker_id=worker_id, enforce_transition=False)
            self.message_repo.update_status(message_record.id, MessageState.RECONCILIATION, result_code="AMBIGUOUS")
            if self.reconciliation_service:
                self.reconciliation_service.enter_reconciliation(
                    task_id=current_task.id,
                    message_id=message_record.id,
                    worker_id=worker_id,
                    session_id=session.session_id if session else None,
                    reason=f"Send outcome unconfirmed: {verification_res.get('reason')}",
                )

            self._record_error(
                current_task,
                ErrorCode.UNKNOWN_RESULT,
                f"Send outcome unconfirmed: {verification_res.get('reason')}",
                retryable=False,
                worker_id=worker_id,
            )
            return False

    def _schedule_next_followup(self, task: Task, contact: Contact, sent_at_iso: str) -> None:
        """
        Schedule the next follow-up upon confirmation of prior message send:
        Delegates to the authoritative FollowupService.
        """
        if getattr(self, "followup_service", None) is not None:
            self.followup_service.schedule_next_followup(task, contact, sent_at_iso)
            return

        try:
            sent_dt = datetime.fromisoformat(sent_at_iso.replace("Z", "+00:00"))
        except Exception:
            sent_dt = datetime.now(timezone.utc)

        if task.type == TaskType.MESSAGE:
            fu1 = self.followup_repo.get_by_contact_and_sequence(contact.id, 1)
            if fu1 and fu1.status == FollowupStatus.PENDING:
                delay = fu1.delay_seconds or self.settings.followup_1_delay
                sched_dt = sent_dt + timedelta(seconds=delay)
                sched_iso = sched_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                self.followup_repo.schedule(fu1.id, sched_iso)
                logger.info(f"Scheduled follow-up 1 for contact {contact.id} at {sched_iso}")

        elif task.type == TaskType.FOLLOW_UP_1:
            fu2 = self.followup_repo.get_by_contact_and_sequence(contact.id, 2)
            if fu2 and fu2.status == FollowupStatus.PENDING:
                delay = fu2.delay_seconds or self.settings.followup_2_delay
                sched_dt = sent_dt + timedelta(seconds=delay)
                sched_iso = sched_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                self.followup_repo.schedule(fu2.id, sched_iso)
                logger.info(f"Scheduled follow-up 2 for contact {contact.id} at {sched_iso}")

    def _get_or_create_message_record(self, task: Task, contact: Contact) -> Optional[Message]:
        """Fetch the Message record associated with this task or create for follow-up."""
        existing = self.message_repo.get_by_task_id(task.id)
        if existing:
            return existing

        # If this is a follow-up task, get body from Followup record
        if task.type in (TaskType.FOLLOW_UP_1, TaskType.FOLLOW_UP_2):
            seq = 1 if task.type == TaskType.FOLLOW_UP_1 else 2
            fu = self.followup_repo.get_by_contact_and_sequence(contact.id, seq)
            if fu:
                msg = Message(
                    id=generate_id("MSG"),
                    contact_id=contact.id,
                    task_id=task.id,
                    sequence=seq,
                    body=fu.message,
                    status=MessageState.PENDING,
                    created_at=utc_now_iso(),
                    updated_at=utc_now_iso(),
                )
                return self.message_repo.create(msg)

        # Fallback to first message for contact
        contact_messages = self.message_repo.list_by_contact(contact.id)
        return contact_messages[0] if contact_messages else None

    def _record_error(
        self,
        task: Task,
        code: ErrorCode,
        message: str,
        retryable: bool,
        worker_id: Optional[str] = None,
    ) -> None:
        """Persist error record and emit error event."""
        err = ErrorRecord(
            id=generate_id("ERR"),
            code=code,
            message=message,
            severity=ErrorSeverity.MEDIUM if retryable else ErrorSeverity.HIGH,
            retryable=retryable,
            attempt=task.attempt_count,
            task_id=task.id,
        )
        try:
            self.error_repo.record(err)
        except Exception as e:
            logger.error(f"Failed to record error: {e}")

        self.event_repo.record(
            event_code=EventCode.TASK_EXECUTION_FAILED,
            category="messaging",
            level=EventLevel.ERROR,
            entity_type="task",
            entity_id=task.id,
            payload={
                "error_code": code.value,
                "error_message": message,
                "retryable": retryable,
                "worker_id": worker_id,
            },
        )
