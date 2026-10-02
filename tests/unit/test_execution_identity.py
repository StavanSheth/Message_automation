"""Unit tests for atomic ExecutionIdentityRepository persistence and race-condition prevention."""

import pytest
import sqlite3
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.execution_identity_repo import ExecutionIdentityRepository
from backend.repositories.task_repo import TaskRepository
from backend.domain.models import ExecutionIdentity, Task, utc_now_iso
from backend.domain.enums import TaskState, TaskType


@pytest.fixture
def identity_env(tmp_path):
    db_path = str(tmp_path / "test_identity.db")
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()

    task_repo = TaskRepository(db)
    identity_repo = ExecutionIdentityRepository(db)

    from backend.repositories.contact_repo import ContactRepository
    from backend.domain.models import Contact
    contact_repo = ContactRepository(db)
    contact_repo.create(Contact(id="c-1", name="Contact 1", instagram_url="https://instagram.com/c1"))
    contact_repo.create(Contact(id="c-2", name="Contact 2", instagram_url="https://instagram.com/c2"))
    contact_repo.create(Contact(id="c-up", name="Contact UP", instagram_url="https://instagram.com/cup"))

    # Seed parent tasks
    task_repo.create(Task(id="t-1", contact_id="c-1", type=TaskType.MESSAGE, status=TaskState.READY))
    task_repo.create(Task(id="t-2", contact_id="c-2", type=TaskType.MESSAGE, status=TaskState.READY))
    task_repo.create(Task(id="t-up", contact_id="c-up", type=TaskType.MESSAGE, status=TaskState.READY))

    return {
        "db": db,
        "task_repo": task_repo,
        "identity_repo": identity_repo,
    }


def test_atomic_execution_identity_insert(identity_env):
    """Execution identity must insert atomically with all required fields."""
    identity_repo = identity_env["identity_repo"]
    ident = ExecutionIdentity(
        execution_key="k-unique-1",
        task_id="t-1",
        message_id="m-1",
        contact_id="c-1",
        message_hash="hash-1",
        attempt=1,
        worker_id="w-1",
        session_id="s-1",
        correlation_id="corr-1",
        state="RUNNING",
    )
    saved = identity_repo.create(ident)
    assert saved.execution_key == "k-unique-1"

    fetched = identity_repo.get("k-unique-1")
    assert fetched is not None
    assert fetched.task_id == "t-1"
    assert fetched.state == "RUNNING"


def test_duplicate_execution_identity_conflict(identity_env):
    """Attempting to insert a duplicate execution key must raise IntegrityError."""
    identity_repo = identity_env["identity_repo"]
    ident1 = ExecutionIdentity(
        execution_key="k-duplicate-key",
        task_id="t-1",
        message_id="m-1",
        contact_id="c-1",
        message_hash="hash-dup",
        attempt=1,
        worker_id="w-1",
        session_id="s-1",
        correlation_id="corr-1",
        state="RUNNING",
    )
    identity_repo.create(ident1)

    ident2 = ExecutionIdentity(
        execution_key="k-duplicate-key",
        task_id="t-2",
        message_id="m-2",
        contact_id="c-2",
        message_hash="hash-dup",
        attempt=1,
        worker_id="w-2",
        session_id="s-2",
        correlation_id="corr-2",
        state="RUNNING",
    )
    with pytest.raises(sqlite3.IntegrityError):
        identity_repo.create(ident2)


def test_update_execution_identity_state(identity_env):
    """Updating state and outcome transitions execution identity."""
    identity_repo = identity_env["identity_repo"]
    ident = ExecutionIdentity(
        execution_key="k-update-test",
        task_id="t-up",
        message_id="m-up",
        contact_id="c-up",
        message_hash="hash-up",
        attempt=1,
        worker_id="w-1",
        session_id="s-1",
        correlation_id="corr-up",
        state="RUNNING",
    )
    identity_repo.create(ident)

    updated = identity_repo.update_state("k-update-test", state="SENT", outcome="CONFIRMED_SENT")
    assert updated is True

    record = identity_repo.get("k-update-test")
    assert record.state == "SENT"
    assert record.outcome == "CONFIRMED_SENT"
    assert record.completed_at is not None
