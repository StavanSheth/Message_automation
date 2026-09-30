"""Task management application service."""

from typing import Optional, List
from backend.domain.models import Task, utc_now_iso
from backend.domain.enums import TaskState, TaskType, EventCode, EventLevel
from backend.domain.errors import DuplicateTaskError, TaskStateError
from backend.repositories.task_repo import TaskRepository
from backend.repositories.event_repo import EventRepository
from backend.events.correlation import generate_id


class TaskService:
    """Service for managing tasks, state transitions, claiming, and crash recovery."""

    def __init__(self, task_repo: TaskRepository, event_repo: EventRepository):
        self.task_repo = task_repo
        self.event_repo = event_repo

    def create_task(
        self,
        contact_id: str,
        task_type: TaskType,
        sequence: int = 0,
        priority: int = 0,
        scheduled_at: Optional[str] = None,
    ) -> Task:
        """Create a task with strict duplicate prevention."""
        now_iso = utc_now_iso()
        task = Task(
            id=generate_id("TASK"),
            contact_id=contact_id,
            type=task_type,
            sequence=sequence,
            status=TaskState.CREATED,
            priority=priority,
            scheduled_at=scheduled_at or now_iso,
            created_at=now_iso,
            updated_at=now_iso,
        )
        created = self.task_repo.create(task)
        self.event_repo.record(
            event_code=EventCode.TASK_CREATED,
            category="task",
            level=EventLevel.INFO,
            entity_type="task",
            entity_id=created.id,
            payload={
                "contact_id": contact_id,
                "type": task_type.value,
                "sequence": sequence,
            },
        )
        return created

    def transition_state(
        self,
        task_id: str,
        new_state: TaskState,
        worker_id: Optional[str] = None,
        last_error_id: Optional[str] = None,
    ) -> Task:
        """Advance task state according to state machine."""
        task = self.task_repo.update_state(
            task_id=task_id,
            new_state=new_state,
            worker_id=worker_id,
            last_error_id=last_error_id,
            enforce_transition=True,
        )
        self.event_repo.record(
            event_code=EventCode.TASK_STATE_CHANGED,
            category="task",
            level=EventLevel.INFO,
            entity_type="task",
            entity_id=task_id,
            payload={"new_state": new_state.value, "worker_id": worker_id},
        )
        return task

    def claim_task(self, task_id: str, worker_id: str, lock_token: str) -> bool:
        """Attempt to atomically claim a ready/queued task for execution."""
        claimed = self.task_repo.claim_task(task_id, worker_id, lock_token)
        if claimed:
            self.event_repo.record(
                event_code=EventCode.TASK_STARTED,
                category="task",
                level=EventLevel.INFO,
                entity_type="task",
                entity_id=task_id,
                payload={"worker_id": worker_id, "lock_token": lock_token},
            )
        return claimed

    def release_task(self, task_id: str, lock_token: str) -> bool:
        """Release a task lock."""
        return self.task_repo.release_task(task_id, lock_token)

    def recover_interrupted_on_startup(self) -> int:
        """Mark tasks left in RUNNING as INTERRUPTED upon application startup."""
        count = self.task_repo.mark_running_as_interrupted()
        if count > 0:
            self.event_repo.record(
                event_code=EventCode.TASK_INTERRUPTED,
                category="recovery",
                level=EventLevel.WARNING,
                entity_type="task",
                entity_id=None,
                payload={"interrupted_count": count, "trigger": "startup_recovery"},
            )
        return count

    def get_ready_tasks(self, limit: int = 50) -> List[Task]:
        return self.task_repo.list_ready(limit=limit)

    def get_interrupted_tasks(self) -> List[Task]:
        return self.task_repo.list_interrupted()
