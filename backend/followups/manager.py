"""Follow-up Manager contract and service."""

from typing import List, Optional, Tuple
from backend.domain.models import Followup, Task, utc_now_iso
from backend.domain.enums import FollowupStatus, RepliedStatus, TaskType, TaskState
from backend.repositories.followup_repo import FollowupRepository
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.task_repo import TaskRepository
from backend.repositories.event_repo import EventRepository


class FollowupManager:
    """Manages follow-up state, cancellation on reply, and prerequisite eligibility checks."""

    def __init__(
        self,
        followup_repo: FollowupRepository,
        contact_repo: ContactRepository,
        task_repo: TaskRepository,
        event_repo: Optional[EventRepository] = None,
    ):
        self.followup_repo = followup_repo
        self.contact_repo = contact_repo
        self.task_repo = task_repo
        self.event_repo = event_repo

    def can_execute_followup(
        self, followup_id: str, current_time_iso: Optional[str] = None
    ) -> Tuple[bool, str]:
        """
        Evaluate follow-up eligibility rules:
        1. Follow-up record must exist.
        2. Contact must exist.
        3. Contact must not be marked Replied = YES.
        4. Prerequisite task must have completed successfully:
           - For Follow-up 1: Initial MESSAGE task must be COMPLETED.
           - For Follow-up 2: FOLLOW_UP_1 task must be COMPLETED.
        5. Follow-up status must be SCHEDULED or DUE.
        6. Follow-up must be due (scheduled_at <= current_time).
        """
        followup = self.followup_repo.get_by_id(followup_id)
        if not followup:
            return False, "Followup not found"

        contact = self.contact_repo.get_by_id(followup.contact_id)
        if not contact:
            return False, "Contact not found"

        # Rule 3: Replied = YES stops all follow-ups
        if contact.replied_status == RepliedStatus.YES:
            return False, "Contact already replied YES"

        # Rule 5: Status check
        if followup.status not in (FollowupStatus.SCHEDULED, FollowupStatus.DUE):
            return False, f"Followup status is {followup.status.value}, expected SCHEDULED or DUE"

        # Rule 4: Prerequisite check
        if followup.sequence == 1:
            init_task = self.task_repo.get_by_contact_and_type(contact.id, TaskType.MESSAGE, sequence=0)
            if not init_task or init_task.status != TaskState.COMPLETED:
                return False, "Prerequisite initial message has not completed"
        elif followup.sequence == 2:
            fu1_task = self.task_repo.get_by_contact_and_type(contact.id, TaskType.FOLLOW_UP_1, sequence=1)
            if not fu1_task or fu1_task.status != TaskState.COMPLETED:
                return False, "Prerequisite follow-up 1 has not completed"

        # Rule 6: Due time check
        now_iso = current_time_iso or utc_now_iso()
        if followup.scheduled_at > now_iso:
            return False, f"Followup is not yet due (scheduled at {followup.scheduled_at}, current {now_iso})"

        return True, "Eligible"

    def cancel_for_contact(self, contact_id: str, reason: str = "REPLIED") -> int:
        """Cancel pending followups for a contact."""
        return self.followup_repo.cancel_pending_for_contact(contact_id, cancel_reason=reason)
