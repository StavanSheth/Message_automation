"""Recovery tests for power failure and crash reconciliation."""

import pytest
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.task_repo import TaskRepository
from backend.repositories.event_repo import EventRepository
from backend.application.task_service import TaskService
from backend.domain.models import Contact, Task
from backend.domain.enums import TaskType, TaskState


def test_crash_recovery_transitions_running_to_interrupted(tmp_path):
    db_file = str(tmp_path / "crash.db")
    db = DatabaseManager(db_file)
    MigrationRunner(db).apply_pending()

    contact_repo = ContactRepository(db)
    task_repo = TaskRepository(db)
    event_repo = EventRepository(db)
    task_service = TaskService(task_repo, event_repo)

    # 1. Create contact
    contact = Contact(
        id="C-CRASH",
        name="Interrupted User",
        instagram_url="https://instagram.com/interrupted",
    )
    contact_repo.create(contact)

    # 2. Create task and claim it (moving to RUNNING)
    task = Task(
        id="T-RUNNING-1",
        contact_id="C-CRASH",
        type=TaskType.MESSAGE,
        sequence=0,
        status=TaskState.READY,
    )
    task_repo.create(task)
    task_service.claim_task("T-RUNNING-1", worker_id="WORKER-DEAD", lock_token="TOKEN-99")

    current_task = task_repo.get_by_id("T-RUNNING-1")
    assert current_task.status == TaskState.RUNNING

    # 3. Simulate application crash and restart: invoke recover_interrupted_on_startup
    interrupted_count = task_service.recover_interrupted_on_startup()
    assert interrupted_count == 1

    recovered_task = task_repo.get_by_id("T-RUNNING-1")
    assert recovered_task.status == TaskState.INTERRUPTED
    assert recovered_task.lock_token is None  # Lock cleared

    # 4. Interrupted tasks are discoverable for reconciliation
    interrupted_list = task_service.get_interrupted_tasks()
    assert len(interrupted_list) == 1
    assert interrupted_list[0].id == "T-RUNNING-1"
