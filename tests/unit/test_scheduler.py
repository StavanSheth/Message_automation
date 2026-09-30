"""Unit tests for Scheduler foundation service."""

import pytest
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.task_repo import TaskRepository
from backend.repositories.followup_repo import FollowupRepository
from backend.scheduler.service import SchedulerFoundationService
from backend.domain.models import Contact, Followup, Task
from backend.domain.enums import FollowupStatus, RepliedStatus, TaskType, TaskState


@pytest.fixture
def scheduler_fixture(tmp_path):
    db = DatabaseManager(str(tmp_path / "scheduler.db"))
    MigrationRunner(db).apply_pending()
    contact_repo = ContactRepository(db)
    task_repo = TaskRepository(db)
    fu_repo = FollowupRepository(db)
    scheduler = SchedulerFoundationService(task_repo, fu_repo, contact_repo)

    contact = Contact(
        id="C-SCHED-1",
        name="Scheduled User",
        instagram_url="https://instagram.com/sched_user",
        replied_status=RepliedStatus.UNKNOWN,
    )
    contact_repo.create(contact)
    return contact_repo, task_repo, fu_repo, scheduler


def test_scheduler_pause_and_resume(scheduler_fixture):
    _, task_repo, _, scheduler = scheduler_fixture

    task = Task(
        id="T-READY-SCHED",
        contact_id="C-SCHED-1",
        type=TaskType.MESSAGE,
        status=TaskState.READY,
    )
    task_repo.create(task)

    # Active tick returns ready tasks
    assert len(scheduler.tick()) == 1

    # Paused tick returns empty list
    scheduler.pause()
    assert scheduler.is_paused is True
    assert len(scheduler.tick()) == 0

    # Resumed tick returns ready tasks again
    scheduler.resume()
    assert scheduler.is_paused is False
    assert len(scheduler.tick()) == 1


def test_scheduler_due_followup_instantiation(scheduler_fixture):
    contact_repo, task_repo, fu_repo, scheduler = scheduler_fixture

    # Add a follow-up that is already due
    fu = Followup(
        id="FU-DUE-1",
        contact_id="C-SCHED-1",
        sequence=1,
        message="Follow-up message",
        delay_seconds=86400,
        scheduled_at="2020-01-01T00:00:00Z",  # In the past
        status=FollowupStatus.SCHEDULED,
    )
    fu_repo.create(fu)

    # Tick should evaluate due follow-up and create a task for it
    ready_tasks = scheduler.tick()
    assert any(t.type == TaskType.FOLLOW_UP_1 for t in ready_tasks)

    # Follow-up status should now be DUE
    updated_fu = fu_repo.get_by_id("FU-DUE-1")
    assert updated_fu.status == FollowupStatus.DUE
