"""Phase 5 Integration Tests: End-to-End Backup, Verification, and Restore."""

import os
import pytest
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.database.backup import DatabaseBackupService
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.task_repo import TaskRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.event_repo import EventRepository
from backend.domain.models import Contact, Task, Message
from backend.domain.enums import TaskType, TaskState, MessageState, EventCode, EventLevel


@pytest.fixture
def prod_backup_env(tmp_path):
    db_file = str(tmp_path / "live_production.db")
    db = DatabaseManager(db_file)
    MigrationRunner(db).apply_pending()

    contact_repo = ContactRepository(db)
    task_repo = TaskRepository(db)
    msg_repo = MessageRepository(db)
    event_repo = EventRepository(db)

    # Populate realistic production data
    contact_repo.create(Contact(id="c-bkp-1", name="Backup Contact 1", instagram_url="https://instagram.com/bkp1"))
    contact_repo.create(Contact(id="c-bkp-2", name="Backup Contact 2", instagram_url="https://instagram.com/bkp2"))

    task_repo.create(Task(id="task-bkp-1", contact_id="c-bkp-1", type=TaskType.MESSAGE, status=TaskState.COMPLETED, priority=10))
    task_repo.create(Task(id="task-bkp-2", contact_id="c-bkp-2", type=TaskType.MESSAGE, status=TaskState.READY, priority=5))

    msg_repo.create(Message(id="msg-bkp-1", contact_id="c-bkp-1", task_id="task-bkp-1", body="Message 1", status=MessageState.SENT))

    event_repo.record(
        event_code=EventCode.TASK_COMPLETED,
        category="execution",
        level=EventLevel.INFO,
        task_id="task-bkp-1",
        payload={"result": "success"},
    )

    backup_service = DatabaseBackupService(db, default_backup_dir=str(tmp_path / "backups"))
    return {
        "db": db,
        "db_file": db_file,
        "backup_service": backup_service,
        "tmp_path": tmp_path,
    }


def test_full_backup_verification_and_restore_cycle(prod_backup_env):
    """
    Section 17 Mandate:
    DB backup -> backup validation -> restore to temporary DB -> migration/schema verification -> integrity check
    """
    backup_service = prod_backup_env["backup_service"]
    tmp_path = prod_backup_env["tmp_path"]

    # 1. DB backup
    backup_file = backup_service.create_backup()
    assert os.path.exists(backup_file)

    # 2. Backup validation (integrity check + foreign key check + schema verification)
    assert backup_service.verify_backup(backup_file) is True

    # 3. Restore to temporary DB
    restored_db_path = str(tmp_path / "restored_production.db")
    restored_success = backup_service.restore_backup(backup_file, restored_db_path)
    assert restored_success is True

    # 4. Verify restored DB schema and data completeness
    restored_db = DatabaseManager(restored_db_path)
    runner = MigrationRunner(restored_db)
    assert runner.verify_schema() is True
    assert len(runner.get_pending_migrations()) == 0

    restored_contact_repo = ContactRepository(restored_db)
    restored_task_repo = TaskRepository(restored_db)
    restored_msg_repo = MessageRepository(restored_db)
    restored_event_repo = EventRepository(restored_db)

    assert restored_contact_repo.count() == 2
    t1 = restored_task_repo.get_by_id("task-bkp-1")
    assert t1 is not None
    assert t1.status == TaskState.COMPLETED

    m1 = restored_msg_repo.get_by_id("msg-bkp-1")
    assert m1 is not None
    assert m1.status == MessageState.SENT

    events = restored_event_repo.list_events(task_id="task-bkp-1")
    assert len(events) >= 1
