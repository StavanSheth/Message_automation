"""Scheduler service contract and foundation implementation."""

from abc import ABC, abstractmethod
from typing import List, Optional
from backend.domain.models import Task, Followup, utc_now_iso
from backend.domain.enums import TaskState, TaskType, FollowupStatus
from backend.domain.errors import DuplicateTaskError
from backend.repositories.task_repo import TaskRepository
from backend.repositories.followup_repo import FollowupRepository
from backend.repositories.contact_repo import ContactRepository
from backend.events.correlation import generate_id


class SchedulerService(ABC):
    """Contract for task scheduling and due follow-up processing."""

    @abstractmethod
    def start(self) -> None:
        """Start scheduler background loop."""
        pass

    @abstractmethod
    def pause(self) -> None:
        """Pause scheduler loop."""
        pass

    @abstractmethod
    def resume(self) -> None:
        """Resume scheduler loop."""
        pass

    @abstractmethod
    def tick(self) -> List[Task]:
        """Single scheduler evaluation step to dispatch due tasks."""
        pass

    @abstractmethod
    def schedule_followup(self, contact_id: str, sequence: int) -> bool:
        """Schedule a follow-up task when prerequisite completes."""
        pass

    @abstractmethod
    def cancel_followups(self, contact_id: str, reason: str = "REPLIED") -> int:
        """Cancel pending follow-ups for a contact."""
        pass


class SchedulerFoundationService(SchedulerService):
    """
    Phase 1 Scheduler foundation implementing pause/resume state,
    due follow-up conversion, and task querying.
    """

    def __init__(
        self,
        task_repo: TaskRepository,
        followup_repo: FollowupRepository,
        contact_repo: ContactRepository,
    ):
        self.task_repo = task_repo
        self.followup_repo = followup_repo
        self.contact_repo = contact_repo
        self.is_paused: bool = False
        self.is_running: bool = False

    def start(self) -> None:
        self.is_running = True
        self.is_paused = False

    def pause(self) -> None:
        self.is_paused = True

    def resume(self) -> None:
        self.is_paused = False

    def tick(self) -> List[Task]:
        """
        Evaluate due follow-ups and list tasks ready for execution.
        Returns empty list when paused.
        """
        if self.is_paused:
            return []

        now_iso = utc_now_iso()

        # Check due follow-ups and instantiate tasks for them
        due_followups = self.followup_repo.list_due(now_iso)
        for fu in due_followups:
            # Check contact replied state
            contact = self.contact_repo.get_by_id(fu.contact_id)
            if not contact or contact.replied_status.value == "YES":
                self.followup_repo.update_status(fu.id, FollowupStatus.CANCELLED, cancel_reason="REPLIED")
                continue

            task_type = TaskType.FOLLOW_UP_1 if fu.sequence == 1 else TaskType.FOLLOW_UP_2
            existing = self.task_repo.get_by_contact_and_type(fu.contact_id, task_type, sequence=fu.sequence)
            if not existing:
                try:
                    new_task = Task(
                        id=generate_id("TASK"),
                        contact_id=fu.contact_id,
                        type=task_type,
                        sequence=fu.sequence,
                        status=TaskState.READY,
                        scheduled_at=now_iso,
                        created_at=now_iso,
                        updated_at=now_iso,
                    )
                    self.task_repo.create(new_task)
                    self.followup_repo.update_status(fu.id, FollowupStatus.DUE)
                except DuplicateTaskError:
                    pass

        return self.task_repo.list_ready()

    def schedule_followup(self, contact_id: str, sequence: int) -> bool:
        """Schedule a follow-up after prior message confirms."""
        fu = self.followup_repo.get_by_contact_and_sequence(contact_id, sequence)
        if not fu:
            return False
        return self.followup_repo.update_status(fu.id, FollowupStatus.SCHEDULED)

    def cancel_followups(self, contact_id: str, reason: str = "REPLIED") -> int:
        """Cancel pending follow-ups for a contact."""
        return self.followup_repo.cancel_pending_for_contact(contact_id, cancel_reason=reason)
