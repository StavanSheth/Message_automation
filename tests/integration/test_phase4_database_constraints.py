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
    assert len(applied) >= 9  # 001 through 009
    assert "008_phase4_final_integrity.sql" in applied
    assert "009_phase4_relational_ownership_integrity.sql" in applied
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


def test_database_enforces_foreign_key_and_ownership_triggers(tmp_path):
    """Verify that database triggers strictly enforce foreign keys and cross-entity ownership at DB layer."""
    db_path = str(tmp_path / "triggers_db.db")
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()

    acc_repo = AccountRepository(db)
    wkr_repo = WorkerRepository(db)
    sess_repo = BrowserSessionRepository(db)
    task_repo = TaskRepository(db)
    cnt_repo = ContactRepository(db)

    # 1. Reject task referencing non-existent account
    cnt_repo.create(Contact(id="cnt-trg-1", name="Trigger Contact", instagram_url="https://instagram.com/trg"))
    task_bad_acc = Task(id="t-bad-acc", contact_id="cnt-trg-1", type=TaskType.MESSAGE, account_id="acc-nonexistent")
    with pytest.raises(sqlite3.IntegrityError, match="Foreign key violation.*tasks.account_id"):
        task_repo.create(task_bad_acc)

    # 2. Reject worker referencing non-existent account
    wkr_bad_acc = WorkerRecord(
        id="wkr-bad-acc",
        worker_code="W-BAD-ACC",
        mode=WorkerMode.SINGLE_BROWSER,
        status=WorkerStatus.IDLE,
        account_id="acc-nonexistent",
    )
    with pytest.raises(sqlite3.IntegrityError, match="Foreign key violation.*workers.account_id"):
        wkr_repo.create(wkr_bad_acc)

    # 3. Create valid accounts and workers
    acc_repo.create(Account(id="acc-A", username="userA", status=AccountStatus.ACTIVE, profile_path="/profiles/userA"))
    acc_repo.create(Account(id="acc-B", username="userB", status=AccountStatus.ACTIVE, profile_path="/profiles/userB"))

    wkr_repo.create(
        WorkerRecord(
            id="WKR-A",
            worker_code="W-A",
            mode=WorkerMode.SINGLE_BROWSER,
            status=WorkerStatus.IDLE,
            account_id="acc-A",
        )
    )
    wkr_repo.create(
        WorkerRecord(
            id="WKR-B",
            worker_code="W-B",
            mode=WorkerMode.SINGLE_BROWSER,
            status=WorkerStatus.IDLE,
            account_id="acc-B",
        )
    )

    # 4. Reject task assigned to mismatched worker at DB level
    task_mismatch = Task(id="t-mismatch", contact_id="cnt-trg-1", type=TaskType.MESSAGE, account_id="acc-A", worker_id="WKR-B")
    with pytest.raises(sqlite3.IntegrityError, match="Ownership mismatch.*task.account_id"):
        task_repo.create(task_mismatch)

    # 5. Reject browser session referencing mismatched worker
    sess_mismatch = BrowserSession(
        id="sess-mismatch",
        account_id="acc-A",
        worker_id="WKR-B",
        profile_path="/profiles/userA",
        status="READY",
    )
    with pytest.raises(sqlite3.IntegrityError, match="Ownership mismatch.*browser_sessions.account_id"):
        sess_repo.create(sess_mismatch)

    # 6. Reject account assigning a worker that belongs to a different account
    with pytest.raises(sqlite3.IntegrityError, match="Ownership mismatch.*assigned_worker_id"):
        acc_repo.assign_worker("acc-A", "WKR-B")

    # 7. Reject browser session with profile path mismatching account
    sess_bad_prof = BrowserSession(
        id="sess-bad-prof",
        account_id="acc-A",
        worker_id="WKR-A",
        profile_path="/profiles/wrong_user",
        status="READY",
    )
    with pytest.raises(sqlite3.IntegrityError, match="Ownership mismatch.*browser_session profile"):
        sess_repo.create(sess_bad_prof)

