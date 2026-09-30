"""Unit tests for SQLite database manager, migrations, foreign keys, and transactions."""

import sqlite3
import pytest
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner


def test_migrations_create_complete_schema(tmp_path):
    db_file = str(tmp_path / "test.db")
    db = DatabaseManager(db_file)
    runner = MigrationRunner(db)

    # First run should apply 001_initial_schema.sql
    applied = runner.apply_pending()
    assert len(applied) == 1
    assert "001_initial_schema.sql" in applied[0]

    # Verify all expected tables exist
    assert runner.verify_schema() is True

    # Re-running migrations should be idempotent
    re_applied = runner.apply_pending()
    assert len(re_applied) == 0


def test_foreign_keys_enforced(tmp_path):
    db_file = str(tmp_path / "test_fk.db")
    db = DatabaseManager(db_file)
    runner = MigrationRunner(db)
    runner.apply_pending()

    conn = db.get_connection()
    # Inserting a task referencing a non-existent contact must fail with IntegrityError
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction():
            conn.execute(
                """
                INSERT INTO tasks (id, contact_id, type, status, created_at, updated_at)
                VALUES ('T1', 'NON_EXISTENT_CONTACT', 'MESSAGE', 'READY', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
                """
            )


def test_transaction_rollback_on_failure(tmp_path):
    db_file = str(tmp_path / "test_rollback.db")
    db = DatabaseManager(db_file)
    runner = MigrationRunner(db)
    runner.apply_pending()

    # Create a contact first
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO contacts (id, name, instagram_url, replied_status, created_at, updated_at)
            VALUES ('C1', 'Test User', 'https://instagram.com/test', 'UNKNOWN', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
            """
        )

    # Begin transaction that fails halfway
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO tasks (id, contact_id, type, status, created_at, updated_at)
                VALUES ('T_ROLLBACK', 'C1', 'MESSAGE', 'READY', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
                """
            )
            # Deliberately raise exception
            raise RuntimeError("Forced transaction failure")

    # Verify task was rolled back
    conn = db.get_connection()
    row = conn.execute("SELECT * FROM tasks WHERE id = 'T_ROLLBACK';").fetchone()
    assert row is None
