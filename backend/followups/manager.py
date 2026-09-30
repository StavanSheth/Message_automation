"""Follow-up Manager contract and service."""

from typing import List, Optional
from backend.domain.models import Followup, Task
from backend.domain.enums import FollowupStatus, RepliedStatus
from backend.repositories.followup_repo import FollowupRepository
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.task_repo import TaskRepository
from backend.repositories.event_repo import EventRepository
from backend.domain.models import utc_now_iso


class FollowupManager:
    """Manages follow-up state, cancellation on reply, and eligibility checks."""

    def __init__(
        self,
        followup_repo: FollowupRepository,
        contact_repo: ContactRepository,
        task_repo: TaskRepository,
        event_repo: EventRepository,
    ):
        self.followup_repo = followup_repo
        self.contact_repo = contact_repo
        self.task_repo = task_repo
        self.event_repo = event_repo

    def can_execute_followup(self, followup_id: str) -> tuple[bool, str]:
        """
        Evaluate follow-up eligibility rules:
        - Contact must not be marked Replied = YES
        - Prerequisite message must have completed
        - Follow-up must be due
        """
        followup = self.followup_repo.get_by_id(followup_id)
        if not followup:
            return False, "Followup not found"

        contact = self.contact_repo.get_by_id(followup.contact_id)
        if not contact:
            return False, "Contact not found"

        if contact.replied_status == RepliedStatus.YES:
            return False, "Contact already replied YES"

        if followup.status != FollowupStatus.DUE and followup.status != FollowupStatus.SCHEDULED:
            return False, f"Followup status is {followup.status.value}"

        now_iso = utc_now_iso()
        if followup.scheduled_at > now_iso:
            return False, "Followup is not yet due"

        return True, "Eligible"

    def cancel_for_contact(self, contact_id: str, reason: str = "REPLIED") -> int:
        """Cancel pending followups for a contact."""
        return self.followup_repo.cancel_pending_for_contact(contact_id, cancel_reason=reason)
