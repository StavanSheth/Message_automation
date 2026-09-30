"""Unit tests for Task state transitions, duplicate prevention, and locking."""

import pytest
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.task_repo import TaskRepository
from backend.domain.models import Contact, Task
from backend.domain.enums import TaskType, TaskState
from backend.domain.errors import DuplicateTaskError, TaskStateError


@pytest.fixture
def repos(tmp_path):
    db = DatabaseManager(str(tmp_path / "tasks.db"))
    MigrationRunner(db).apply_pending()
    contact_repo = ContactRepository(db)
    task_repo = TaskRepository(db)

    # Seed contact
    contact = Contact(
        id="C-TEST",
        name="Target User",
        instagram_url="https://instagram.com/target",
    )
    contact_repo.create(contact)
    return contact_repo, task_repo


def test_task_creation_and_state_transitions(repos):
    _, task_repo = repos
    task = Task(
        id="T-001",
        contact_id="C-TEST",
        type=TaskType.MESSAGE,
        sequence=0,
        status=TaskState.CREATED,
    )
    task_repo.create(task)

    # Valid transition: CREATED -> VALIDATING -> QUEUED -> READY -> RUNNING -> COMPLETED
    t = task_repo.update_state("T-001", TaskState.VALIDATING)
    assert t.status == TaskState.VALIDATING

    t = task_repo.update_state("T-001", TaskState.QUEUED)
    assert t.status == TaskState.QUEUED

    t = task_repo.update_state("T-001", TaskState.READY)
    assert t.status == TaskState.READY

    t = task_repo.update_state("T-001", TaskState.RUNNING, worker_id="W-1")
    assert t.status == TaskState.RUNNING
    assert t.worker_id == "W-1"
    assert t.started_at is not None

    t = task_repo.update_state("T-001", TaskState.COMPLETED)
    assert t.status == TaskState.COMPLETED
    assert t.completed_at is not None


def test_illegal_state_transition_raises_error(repos):
    _, task_repo = repos
    task = Task(
        id="T-002",
        contact_id="C-TEST",
        type=TaskType.FOLLOW_UP_1,
        sequence=1,
        status=TaskState.CREATED,
    )
    task_repo.create(task)

    # Directly jumping from CREATED to COMPLETED is prohibited
    with pytest.raises(TaskStateError):
        task_repo.update_state("T-002", TaskState.COMPLETED)


def test_task_duplicate_prevention(repos):
    _, task_repo = repos
    task1 = Task(
        id="T-003",
        contact_id="C-TEST",
        type=TaskType.MESSAGE,
        sequence=0,
        status=TaskState.READY,
    )
    task_repo.create(task1)

    # Creating a second task with the same (contact_id, type, sequence) MUST fail
    task2 = Task(
        id="T-004",
        contact_id="C-TEST",
        type=TaskType.MESSAGE,
        sequence=0,
        status=TaskState.READY,
    )
    with pytest.raises(DuplicateTaskError):
        task_repo.create(task2)


def test_task_atomic_locking_and_release(repos):
    _, task_repo = repos
    task = Task(
        id="T-005",
        contact_id="C-TEST",
        type=TaskType.MESSAGE,
        sequence=0,
        status=TaskState.READY,
    )
    task_repo.create(task)

    # Claim task
    claimed = task_repo.claim_task("T-005", worker_id="WORKER-01", lock_token="TOKEN-XYZ")
    assert claimed is True

    # Re-claiming with another worker must fail
    claimed_again = task_repo.claim_task("T-005", worker_id="WORKER-02", lock_token="TOKEN-ABC")
    assert claimed_again is False

    # Release lock
    released = task_repo.release_task("T-005", lock_token="TOKEN-XYZ")
    assert released is True

    t = task_repo.get_by_id("T-005")
    assert t.lock_token is None


def test_ready_and_interrupted_query(repos):
    _, task_repo = repos
    task1 = Task(
        id="T-READY-1",
        contact_id="C-TEST",
        type=TaskType.MESSAGE,
        priority=10,
        status=TaskState.READY,
    )
    task_repo.create(task1)

    ready_tasks = task_repo.list_ready()
    assert any(t.id == "T-READY-1" for t in ready_tasks)

    # Move to RUNNING
    task_repo.claim_task("T-READY-1", worker_id="W1", lock_token="LOK")

    # Simulate crash recovery: mark running as interrupted
    count = task_repo.mark_running_as_interrupted()
    assert count >= 1

    interrupted = task_repo.list_interrupted()
    assert any(t.id == "T-READY-1" for t in interrupted)
