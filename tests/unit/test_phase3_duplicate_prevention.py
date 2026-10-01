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


def test_deterministic_execution_identity_properties():
    from backend.domain.identity import build_execution_identity, compute_execution_key, compute_message_hash

    hash_a = compute_message_hash("Hello world")
    hash_b = compute_message_hash("Hello universe")

    # 1. Same task + same message + same attempt -> same execution ID
    key1 = compute_execution_key("C-1", "T-1", hash_a, attempt=1)
    key2 = compute_execution_key("C-1", "T-1", hash_a, attempt=1)
    assert key1 == key2
    assert len(key1) == 64

    # 2. Same task + different attempt -> different execution ID
    key_att2 = compute_execution_key("C-1", "T-1", hash_a, attempt=2)
    assert key1 != key_att2

    # 3. Same task + different message -> different execution ID
    key_msg_b = compute_execution_key("C-1", "T-1", hash_b, attempt=1)
    assert key1 != key_msg_b

    # 4. Canonical builder produces matching key
    ident = build_execution_identity(contact_id="C-1", task_id="T-1", message_hash=hash_a, attempt=1)
    assert ident.execution_key == key1


def test_database_enforces_execution_key_uniqueness(db_env):
    db, task_repo, _ = db_env
    from backend.repositories.execution_identity_repo import ExecutionIdentityRepository
    from backend.domain.identity import build_execution_identity, compute_message_hash

    task = Task(id="T-UNIQUE-1", contact_id="C-1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)

    repo = ExecutionIdentityRepository(db)
    msg_hash = compute_message_hash("Payload body")
    ident1 = build_execution_identity(contact_id="C-1", task_id="T-UNIQUE-1", message_hash=msg_hash, attempt=1, worker_id="W-1")
    repo.create(ident1)

    # Re-inserting identical execution key must fail with IntegrityError
    ident2 = build_execution_identity(contact_id="C-1", task_id="T-UNIQUE-1", message_hash=msg_hash, attempt=1, worker_id="W-2")
    with pytest.raises(sqlite3.IntegrityError):
        repo.create(ident2)


def test_execution_service_two_workers_only_one_can_execute(db_env):
    from unittest.mock import MagicMock
    from backend.application.execution_service import ExecutionService
    from backend.repositories.execution_identity_repo import ExecutionIdentityRepository
    from backend.domain.identity import compute_message_hash

    db, task_repo, msg_repo = db_env
    exec_id_repo = ExecutionIdentityRepository(db)
    auto_svc = MagicMock()

    task = Task(id="T-CONC-1", contact_id="C-1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)
    msg = Message(id="M-CONC-1", contact_id="C-1", task_id="T-CONC-1", sequence=0, body="Unique payload", status=MessageState.PENDING)
    msg_repo.create(msg)

    # Worker 1 acquires lease
    lease_1 = task_repo.acquire_lease("T-CONC-1", "W-1", lease_duration_seconds=300)
    assert lease_1 is not None

    service = ExecutionService(
        task_repo=task_repo,
        message_repo=msg_repo,
        automation_service=auto_svc,
        execution_identity_repo=exec_id_repo,
    )

    auto_svc.execute_messaging_task.return_value = True

    # Worker 1 executes
    session_1 = MagicMock()
    success_1 = service.execute_task("T-CONC-1", lease_id=lease_1, worker_id="W-1", session=session_1)
    assert success_1 is True
    assert auto_svc.execute_messaging_task.call_count == 1

    # Worker 2 now tries duplicate execution attempt on the same task/identity
    session_2 = MagicMock()
    success_2 = service.execute_task("T-CONC-1", lease_id=lease_1, worker_id="W-2", session=session_2)
    assert success_2 is False
    # Crucial invariant: auto_svc.execute_messaging_task MUST NOT be called a second time!
    assert auto_svc.execute_messaging_task.call_count == 1


def test_ambiguous_first_send_blocks_second_worker(db_env):
    from unittest.mock import MagicMock
    from backend.application.execution_service import ExecutionService
    from backend.repositories.execution_identity_repo import ExecutionIdentityRepository
    from backend.domain.identity import build_execution_identity, compute_message_hash

    db, task_repo, msg_repo = db_env
    exec_id_repo = ExecutionIdentityRepository(db)
    auto_svc = MagicMock()

    task = Task(id="T-AMBIG-1", contact_id="C-1", type=TaskType.MESSAGE, status=TaskState.RECONCILING)
    task_repo.create(task)
    msg = Message(id="M-AMBIG-1", contact_id="C-1", task_id="T-AMBIG-1", sequence=0, body="Ambiguous payload", status=MessageState.RECONCILIATION)
    msg_repo.create(msg)

    # Record persistent execution identity as RECONCILIATION
    msg_hash = compute_message_hash("Ambiguous payload")
    ident = build_execution_identity(
        contact_id="C-1",
        task_id="T-AMBIG-1",
        message_hash=msg_hash,
        attempt=1,
        worker_id="W-1",
        state="RECONCILIATION",
    )
    exec_id_repo.create(ident)

    service = ExecutionService(
        task_repo=task_repo,
        message_repo=msg_repo,
        automation_service=auto_svc,
        execution_identity_repo=exec_id_repo,
    )

    # Worker 2 tries to send
    session = MagicMock()
    result = service.execute_task("T-AMBIG-1", lease_id="L-ANY", worker_id="W-2", session=session)
    assert result is False
    assert auto_svc.execute_send.call_count == 0

