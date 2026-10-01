"""Phase 3 unit tests for Message Idempotency and Duplicate-Send Prevention (Workstream D)."""

import pytest
import sqlite3
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.task_repo import TaskRepository
from backend.repositories.message_repo import MessageRepository
from backend.domain.models import Task, Message, Contact, utc_now_iso
from backend.domain.enums import TaskState, MessageState, TaskType, RepliedStatus


@pytest.fixture
def db_env(tmp_path):
    db = DatabaseManager(str(tmp_path / "test_idempotency.db"))
    MigrationRunner(db).apply_pending()
    contact_repo = ContactRepository(db)
    contact_repo.create(Contact(id="C-1", name="Test User", instagram_url="https://instagram.com/test_user", replied_status=RepliedStatus.UNKNOWN))
    task_repo = TaskRepository(db)
    msg_repo = MessageRepository(db)
    return db, task_repo, msg_repo


def test_db_level_unique_sent_constraint_prevents_two_sent_messages(db_env):
    db, task_repo, msg_repo = db_env

    task = Task(id="T-IDEMP-1", contact_id="C-1", type=TaskType.MESSAGE, status=TaskState.COMPLETED)
    task_repo.create(task)

    # First SENT message
    msg1 = Message(
        id="M-SENT-1",
        contact_id="C-1",
        task_id="T-IDEMP-1",
        sequence=0,
        body="Hello first",
        status=MessageState.SENT,
        confirmed_at=utc_now_iso(),
    )
    msg_repo.create(msg1)

    # Second message for the same task attempting status = 'SENT' MUST fail at database constraint
    msg2 = Message(
        id="M-SENT-2",
        contact_id="C-1",
        task_id="T-IDEMP-1",
        sequence=0,
        body="Hello duplicate",
        status=MessageState.SENT,
        confirmed_at=utc_now_iso(),
    )

    with pytest.raises(sqlite3.IntegrityError):
        msg_repo.create(msg2)


def test_has_confirmed_sent_message_query(db_env):
    _, task_repo, msg_repo = db_env

    task = Task(id="T-IDEMP-2", contact_id="C-1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)

    assert msg_repo.has_confirmed_sent_message("T-IDEMP-2") is False

    # Create pending message
    msg = Message(id="M-PENDING-1", contact_id="C-1", task_id="T-IDEMP-2", sequence=0, body="Text", status=MessageState.PENDING)
    msg_repo.create(msg)
    assert msg_repo.has_confirmed_sent_message("T-IDEMP-2") is False

    # Update to SENT
    msg_repo.update_status("M-PENDING-1", MessageState.SENT, confirmed_at=utc_now_iso())
    assert msg_repo.has_confirmed_sent_message("T-IDEMP-2") is True


def test_is_in_reconciliation_query(db_env):
    _, task_repo, msg_repo = db_env

    task = Task(id="T-IDEMP-3", contact_id="C-1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)

    assert msg_repo.is_in_reconciliation("T-IDEMP-3") is False

    msg = Message(id="M-REC-1", contact_id="C-1", task_id="T-IDEMP-3", sequence=0, body="Text", status=MessageState.RECONCILIATION)
    msg_repo.create(msg)

    assert msg_repo.is_in_reconciliation("T-IDEMP-3") is True
