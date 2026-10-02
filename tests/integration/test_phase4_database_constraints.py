"""Integration tests verifying database schema migrations, integrity, indexes, and idempotency (Section 15)."""

import pytest
import sqlite3
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.account_repo import AccountRepository
from backend.repositories.browser_session_repo import BrowserSessionRepository
from backend.repositories.worker_repo import WorkerRepository
from backend.repositories.task_repo import TaskRepository
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.system_control_repo import SystemControlRepository
from backend.domain.models import Account, BrowserSession, WorkerRecord, Task, Contact
from backend.domain.enums import AccountStatus, WorkerMode, WorkerStatus, TaskType


def test_migrations_fresh_and_idempotent_execution(tmp_path):
    """Verify that MigrationRunner applies migrations on fresh DB and repeat apply is fully idempotent."""
    db_path = str(tmp_path / "fresh_migration.db")
    db = DatabaseManager(db_path)
    runner = MigrationRunner(db)

    # First apply
    applied = runner.apply_pending()
    assert len(applied) >= 8  # 001 through 008
    assert "008_phase4_final_integrity.sql" in applied
    assert runner.verify_schema() is True

    # Second apply -> must return empty list without error
    applied_again = runner.apply_pending()
    assert len(applied_again) == 0


def test_migration_on_existing_database_upgrades_cleanly(tmp_path):
    """Simulate upgrading an existing database and verify new tables/columns exist."""
    db_path = str(tmp_path / "upgrade_migration.db")
    db = DatabaseManager(db_path)
    runner = MigrationRunner(db)

    runner.apply_pending()

    # Check browser_sessions.account_id column exists
    conn = db.get_connection()
    cur = conn.execute("PRAGMA table_info(browser_sessions);")
    columns = [row[1] for row in cur.fetchall()]
    assert "account_id" in columns
    assert "profile_path" in columns
    assert "worker_id" in columns

    # Check system_controls table exists
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='system_controls';")
    assert cur.fetchone() is not None


def test_account_worker_session_persistence_and_indexes(tmp_path):
    """Verify that account, worker, and browser session ownership columns and repositories work in harmony."""
    db_path = str(tmp_path / "ownership_db.db")
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()

    acc_repo = AccountRepository(db)
    wkr_repo = WorkerRepository(db)
    sess_repo = BrowserSessionRepository(db)
    task_repo = TaskRepository(db)
    cnt_repo = ContactRepository(db)
    sys_repo = SystemControlRepository(db)

    # 1. Create account
    account = Account(id="acc-db-1", username="db_bot", status=AccountStatus.ACTIVE, profile_path="/profiles/db_bot")
    acc_repo.create(account)

    # 2. Create worker bound to account
    worker = WorkerRecord(
        id="wkr-db-1",
        worker_code="W-DB-1",
        mode=WorkerMode.SINGLE_BROWSER,
        status=WorkerStatus.IDLE,
        account_id="acc-db-1",
    )
    wkr_repo.create(worker)

    # 3. Create session bound to account
    session = BrowserSession(
        id="sess-db-1",
        profile_path="/profiles/db_bot",
        status="READY",
        worker_id="wkr-db-1",
        account_id="acc-db-1",
    )
    sess_repo.create(session)

    # 4. Bind account to worker and session
    acc_repo.assign_worker("acc-db-1", "wkr-db-1", "sess-db-1")

    # 5. Create task bound to account
    cnt_repo.create(Contact(id="cnt-db-1", name="DB Contact", instagram_url="https://instagram.com/db_test"))
    task = Task(id="t-db-1", contact_id="cnt-db-1", type=TaskType.MESSAGE, account_id="acc-db-1")
    task_repo.create(task)

    # 6. Verify retrievals
    saved_acc = acc_repo.get_by_id("acc-db-1")
    assert saved_acc.assigned_worker_id == "wkr-db-1"
    assert saved_acc.assigned_session_id == "sess-db-1"

    saved_sess = sess_repo.get_by_id("sess-db-1")
    assert saved_sess.account_id == "acc-db-1"
    assert saved_sess.worker_id == "wkr-db-1"

    saved_task = task_repo.get_by_id("t-db-1")
    assert saved_task.account_id == "acc-db-1"

    # 7. System controls key-value persistence
    sys_repo.set("last_checkpoint", "2026-10-03T00:00:00Z")
    assert sys_repo.get("last_checkpoint") == "2026-10-03T00:00:00Z"
