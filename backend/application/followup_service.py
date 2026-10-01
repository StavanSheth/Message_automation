"""Authoritative Follow-up Service enforcing sequential execution and reply cancellation."""

from datetime import datetime, timezone, timedelta
from typing import Optional, List
from backend.domain.models import Task, Contact, Followup, Message, utc_now_iso
from backend.domain.enums import TaskType, TaskState, FollowupStatus, MessageState, EventCode, EventLevel
from backend.repositories.task_repo import TaskRepository
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.followup_repo import FollowupRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.event_repo import EventRepository
from backend.config.settings import AppSettings, get_settings
from backend.events.correlation import generate_id
from backend.events.logger import get_logger

logger = get_logger("followup_service")


class FollowupService:
    """
    Authoritative service governing follow-up scheduling, prerequisite verification,
    concurrency-safe materialization, and reply cancellation.
    """

    def __init__(
        self,
        followup_repo: FollowupRepository,
        task_repo: TaskRepository,
        contact_repo: ContactRepository,
        message_repo: MessageRepository,
        event_repo: Optional[EventRepository] = None,
        settings: Optional[AppSettings] = None,
    ):
        self.followup_repo = followup_repo
        self.task_repo = task_repo
        self.contact_repo = contact_repo
        self.message_repo = message_repo
        self.event_repo = event_repo
        self.settings = settings or get_settings()

    def schedule_next_followup(
        self,
        completed_task: Task,
        contact: Contact,
        sent_at_iso: str,
    ) -> Optional[Followup]:
        """
        Schedule the next sequence follow-up only after confirmation of prerequisite task completion:
        - If task was MESSAGE (initial) -> schedule FOLLOW_UP_1
        - If task was FOLLOW_UP_1 -> schedule FOLLOW_UP_2
        """
        # Guard: If contact already replied, do not schedule follow-up
        if contact.replied_status.value == "YES":
            logger.info(f"Contact {contact.id} already replied; skipping follow-up scheduling")
            self.followup_repo.cancel_pending_for_contact(contact.id, cancel_reason="REPLIED")
            return None

        try:
            sent_dt = datetime.fromisoformat(sent_at_iso.replace("Z", "+00:00"))
        except Exception:
            sent_dt = datetime.now(timezone.utc)

        target_seq: Optional[int] = None
        delay_seconds: int = 0

        if completed_task.type == TaskType.MESSAGE:
            target_seq = 1
            delay_seconds = getattr(self.settings, "followup_1_delay", 432000)
        elif completed_task.type == TaskType.FOLLOW_UP_1:
            target_seq = 2
            delay_seconds = getattr(self.settings, "followup_2_delay", 432000)

        if target_seq is None:
            return None

        fu = self.followup_repo.get_by_contact_and_sequence(contact.id, target_seq)
        if not fu or fu.status != FollowupStatus.PENDING:
            return None

        actual_delay = fu.delay_seconds if fu.delay_seconds > 0 else delay_seconds
        sched_dt = sent_dt + timedelta(seconds=actual_delay)
        sched_iso = sched_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        scheduled = self.followup_repo.schedule(fu.id, sched_iso)
        if scheduled:
            if self.event_repo:
                self.event_repo.record(
                    event_code=EventCode.FOLLOWUP_CREATED,
                    category="followup",
                    level=EventLevel.INFO,
                    entity_type="followup",
                    entity_id=fu.id,
                    payload={"contact_id": contact.id, "sequence": target_seq, "scheduled_at": sched_iso},
                )
            logger.info(f"Scheduled follow-up {target_seq} for contact {contact.id} at {sched_iso}")
            return self.followup_repo.get_by_id(fu.id)

        return None

    def materialize_due_followups(self) -> List[Task]:
        """
        Atomically materialize due follow-ups into actionable Task records.
        Guarantees:
        1. Cancels followups immediately if contact has replied.
        2. Validates prerequisite task completed and sent before materializing.
        3. Atomically claims followup record to prevent concurrent tick duplicates.
        """
        due_followups = self.followup_repo.list_due()
        created_tasks: List[Task] = []
        now_iso = utc_now_iso()

        for fu in due_followups:
            contact = self.contact_repo.get_by_id(fu.contact_id)
            if not contact:
                continue

            # 1. Reply check: cancel if contact has replied
            replied_val = getattr(contact.replied_status, "value", contact.replied_status)
            if replied_val == "YES":
                logger.info(f"Cancelling due follow-up {fu.id} because contact {contact.id} replied")
                self.followup_repo.cancel(fu.id, reason="REPLIED")
                if self.event_repo:
                    self.event_repo.record(
                        event_code=EventCode.FOLLOWUP_CANCELLED,
                        category="followup",
                        level=EventLevel.INFO,
                        entity_type="followup",
                        entity_id=fu.id,
                        payload={"contact_id": contact.id, "reason": "REPLIED"},
                    )
                continue

            # 2. Prerequisite check: if prior task exists in DB, ensure it was completed and sent
            if fu.sequence == 1:
                prereq_task = self.task_repo.get_by_contact_and_type(contact.id, TaskType.MESSAGE, sequence=0)
                if prereq_task and prereq_task.status != TaskState.COMPLETED:
                    continue
                if prereq_task and self.message_repo:
                    prereq_msg = self.message_repo.get_by_task_id(prereq_task.id)
                    if prereq_msg and prereq_msg.status != MessageState.SENT:
                        continue
            elif fu.sequence == 2:
                prereq_task = self.task_repo.get_by_contact_and_type(contact.id, TaskType.FOLLOW_UP_1, sequence=1)
                if prereq_task and prereq_task.status != TaskState.COMPLETED:
                    continue
                if prereq_task and self.message_repo:
                    prereq_msg = self.message_repo.get_by_task_id(prereq_task.id)
                    if prereq_msg and prereq_msg.status != MessageState.SENT:
                        continue

            # 3. Atomic claim for materialization
            claimed = self.followup_repo.claim_for_materialization(fu.id)
            if not claimed:
                continue

            task_type = TaskType.FOLLOW_UP_1 if fu.sequence == 1 else TaskType.FOLLOW_UP_2

            # Check if task already exists
            existing_task = self.task_repo.get_by_contact_and_type(contact.id, task_type, sequence=fu.sequence)
            if existing_task:
                continue

            new_task = Task(
                id=generate_id("TASK"),
                contact_id=contact.id,
                type=task_type,
                sequence=fu.sequence,
                status=TaskState.READY,
                priority=1,
                scheduled_at=now_iso,
                created_at=now_iso,
                updated_at=now_iso,
            )

            try:
                created = self.task_repo.create(new_task)
                created_tasks.append(created)

                # Create message record for follow-up
                msg = Message(
                    id=generate_id("MSG"),
                    contact_id=contact.id,
                    task_id=created.id,
                    sequence=fu.sequence,
                    body=fu.message,
                    status=MessageState.PENDING,
                    created_at=now_iso,
                    updated_at=now_iso,
                )
                self.message_repo.create(msg)

                if self.event_repo:
                    self.event_repo.record(
                        event_code=EventCode.TASK_CREATED,
                        category="scheduler",
                        level=EventLevel.INFO,
                        entity_type="task",
                        entity_id=created.id,
                        payload={"type": task_type.value, "sequence": fu.sequence, "contact_id": contact.id},
                    )

                logger.info(f"Materialized follow-up task {created.id} for contact {contact.id}")
            except Exception as e:
                logger.error(f"Failed to create task for due follow-up {fu.id}: {e}")

        return created_tasks

    def cancel_pending_followups(self, contact_id: str, reason: str = "REPLIED") -> int:
        """Cancel all pending or scheduled follow-ups for a contact upon reply or manual action."""
        count = self.followup_repo.cancel_pending_for_contact(contact_id, cancel_reason=reason)
        if count > 0 and self.event_repo:
            self.event_repo.record(
                event_code=EventCode.FOLLOWUP_CANCELLED,
                category="followup",
                level=EventLevel.INFO,
                entity_type="contact",
                entity_id=contact_id,
                payload={"reason": reason, "cancelled_count": count},
            )
        return count
