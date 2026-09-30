"""Unit tests for Followup repository and manager rules."""

import pytest
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.followup_repo import FollowupRepository
from backend.repositories.task_repo import TaskRepository
from backend.followups.manager import FollowupManager
from backend.domain.models import Contact, Followup, Task
from backend.domain.enums import FollowupStatus, RepliedStatus, TaskType, TaskState


@pytest.fixture
def repos(tmp_path):
    db = DatabaseManager(str(tmp_path / "followups.db"))
    MigrationRunner(db).apply_pending()
    contact_repo = ContactRepository(db)
    fu_repo = FollowupRepository(db)
    task_repo = TaskRepository(db)
    manager = FollowupManager(fu_repo, contact_repo, task_repo)

    contact = Contact(
        id="C-FU-1",
        name="Followup User",
        instagram_url="https://instagram.com/fu_user",
        replied_status=RepliedStatus.UNKNOWN,
    )
    contact_repo.create(contact)
    return contact_repo, fu_repo, task_repo, manager


def test_followup_create_and_query(repos):
    _, fu_repo, _, _ = repos
    fu1 = Followup(
        id="FU-1",
        contact_id="C-FU-1",
        sequence=1,
        message="Follow-up 1 text",
        delay_seconds=86400,
        scheduled_at="2026-01-02T00:00:00Z",
        status=FollowupStatus.SCHEDULED,
    )
    fu_repo.create(fu1)

    fu2 = Followup(
        id="FU-2",
        contact_id="C-FU-1",
        sequence=2,
        message="Follow-up 2 text",
        delay_seconds=172800,
        scheduled_at="2026-01-04T00:00:00Z",
        status=FollowupStatus.PENDING,
    )
    fu_repo.create(fu2)

    contact_fus = fu_repo.list_by_contact("C-FU-1")
    assert len(contact_fus) == 2
    assert contact_fus[0].sequence == 1
    assert contact_fus[1].sequence == 2


def test_cancel_pending_followups_for_contact(repos):
    _, fu_repo, _, _ = repos
    fu1 = Followup(
        id="FU-CANCEL-1",
        contact_id="C-FU-1",
        sequence=1,
        message="Follow-up message",
        delay_seconds=86400,
        scheduled_at="2026-01-02T00:00:00Z",
        status=FollowupStatus.SCHEDULED,
    )
    fu_repo.create(fu1)

    # Cancel on reply
    cancelled = fu_repo.cancel_pending_for_contact("C-FU-1", cancel_reason="REPLIED")
    assert cancelled == 1

    updated = fu_repo.get_by_id("FU-CANCEL-1")
    assert updated.status == FollowupStatus.CANCELLED
    assert updated.cancel_reason == "REPLIED"
    assert updated.cancelled_at is not None


def test_followup_manager_eligibility_and_prerequisites(repos):
    contact_repo, fu_repo, task_repo, manager = repos

    # Create Follow-up 1 scheduled at 2026-01-05
    fu = Followup(
        id="FU-ELIG-1",
        contact_id="C-FU-1",
        sequence=1,
        message="Checking in",
        delay_seconds=86400,
        scheduled_at="2026-01-05T00:00:00Z",
        status=FollowupStatus.SCHEDULED,
    )
    fu_repo.create(fu)

    # 1. Prerequisite initial task does not exist -> ineligible
    can_exec, reason = manager.can_execute_followup("FU-ELIG-1", current_time_iso="2026-01-06T00:00:00Z")
    assert can_exec is False
    assert "Prerequisite" in reason

    # 2. Initial task exists but is RUNNING (not COMPLETED) -> ineligible
    init_task = Task(
        id="T-INIT-1",
        contact_id="C-FU-1",
        type=TaskType.MESSAGE,
        sequence=0,
        status=TaskState.RUNNING,
    )
    task_repo.create(init_task)
    can_exec, reason = manager.can_execute_followup("FU-ELIG-1", current_time_iso="2026-01-06T00:00:00Z")
    assert can_exec is False
    assert "Prerequisite" in reason

    # 3. Mark initial task COMPLETED, but current_time is BEFORE scheduled_at -> not yet due
    task_repo.update_state("T-INIT-1", TaskState.COMPLETED)
    can_exec, reason = manager.can_execute_followup("FU-ELIG-1", current_time_iso="2026-01-04T00:00:00Z")
    assert can_exec is False
    assert "not yet due" in reason

    # 4. Due date reached and prerequisite completed -> eligible!
    can_exec, reason = manager.can_execute_followup("FU-ELIG-1", current_time_iso="2026-01-05T00:00:00Z")
    assert can_exec is True
    assert reason == "Eligible"

    # 5. User changes Replied = YES -> immediately blocked
    contact_repo.update_replied_status("C-FU-1", RepliedStatus.YES)
    can_exec, reason = manager.can_execute_followup("FU-ELIG-1", current_time_iso="2026-01-06T00:00:00Z")
    assert can_exec is False
    assert "already replied YES" in reason
