"""Unit tests for DatabaseBackupService validating online backup, schema verification, and restore."""

import os
import sqlite3
import pytest
from pathlib import Path
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.database.backup import DatabaseBackupService
from backend.repositories.task_repo import TaskRepository
from backend.repositories.contact_repo import ContactRepository
from backend.domain.models import Task, Contact
from backend.domain.enums import TaskType


@pytest.fixture
def db_with_data(tmp_path):
    db_file = str(tmp_path / "source.db")
    db = DatabaseManager(db_file)
    MigrationRunner(db).apply_pending()

    contact_repo = ContactRepository(db)
    contact_repo.create(Contact(id="c-1", name="Contact 1", instagram_url="https://instagram.com/c1"))
    contact_repo.create(Contact(id="c-2", name="Contact 2", instagram_url="https://instagram.com/c2"))

    task_repo = TaskRepository(db)
    task_repo.create(Task(id="task-backup-1", contact_id="c-1", type=TaskType.MESSAGE, priority=10))
    task_repo.create(Task(id="task-backup-2", contact_id="c-2", type=TaskType.MESSAGE, priority=5))
    return db, db_file, tmp_path


def test_create_and_verify_backup(db_with_data):
    db, db_file, tmp_path = db_with_data
    backup_service = DatabaseBackupService(db, default_backup_dir=str(tmp_path / "backups"))

    backup_path = backup_service.create_backup()
    assert os.path.exists(backup_path)

    # Verify backup
    is_valid = backup_service.verify_backup(backup_path)
    assert is_valid is True


def test_verify_backup_detects_corrupted_file(tmp_path):
    corrupt_file = str(tmp_path / "corrupt.db")
    with open(corrupt_file, "w") as f:
        f.write("NOT_A_VALID_SQLITE_DATABASE_HEADER")

    dummy_db = DatabaseManager(str(tmp_path / "dummy.db"))
    backup_service = DatabaseBackupService(dummy_db)
    assert backup_service.verify_backup(corrupt_file) is False


def test_restore_backup_to_temporary_db(db_with_data):
    db, db_file, tmp_path = db_with_data
    backup_service = DatabaseBackupService(db, default_backup_dir=str(tmp_path / "backups"))

    backup_path = backup_service.create_backup()

    restored_db_path = str(tmp_path / "restored.db")
    success = backup_service.restore_backup(backup_path, restored_db_path)
    assert success is True

    # Verify data in restored database
    restored_db = DatabaseManager(restored_db_path)
    restored_task_repo = TaskRepository(restored_db)
    t1 = restored_task_repo.get_by_id("task-backup-1")
    t2 = restored_task_repo.get_by_id("task-backup-2")
    assert t1 is not None
    assert t1.priority == 10
    assert t2 is not None
    assert t2.priority == 5
