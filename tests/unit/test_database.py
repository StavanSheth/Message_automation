"""Unit tests for SQLite database manager, migrations, foreign keys, and transactions."""

import sqlite3
import pytest
from pathlib import Path
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner


def test_migrations_create_complete_schema(tmp_path):
    db_file = str(tmp_path / "test.db")
    db = DatabaseManager(db_file)
    runner = MigrationRunner(db)

    # First run should apply pending migrations
    applied = runner.apply_pending()
    assert len(applied) == 2
    assert "001_initial_schema.sql" in applied[0]
    assert "002_source_records_unique.sql" in applied[1]

    # Verify all expected tables exist
    assert runner.verify_schema() is True

    # Re-running migrations should be idempotent
    re_applied = runner.apply_pending()
    assert len(re_applied) == 0


def test_failed_migration_rolls_back_completely(tmp_path):
    """Verify that a migration with an error does not apply partial tables or record in schema_migrations."""
    db_file = str(tmp_path / "test_failed_mig.db")
    db = DatabaseManager(db_file)

    # Create temporary migrations dir
    mig_dir = tmp_path / "mig"
    mig_dir.mkdir()

    # Create a broken migration file
    bad_migration = mig_dir / "001_bad.sql"
    bad_migration.write_text(
        """
        CREATE TABLE should_be_rolled_back (id INT PRIMARY KEY);
        INSERT INTO non_existent_table VALUES (1);
        """,
        encoding="utf-8",
    )

    runner = MigrationRunner(db, migrations_dir=str(mig_dir))

    with pytest.raises(sqlite3.OperationalError):
        runner.apply_pending()

    # 1. The partial table must NOT exist in the database
    conn = db.get_connection()
    tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()]
    assert "should_be_rolled_back" not in tables

    # 2. Migration must NOT be recorded in schema_migrations
    applied = runner.get_applied_migrations()
    assert "001_bad.sql" not in applied


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


def test_connection_lifecycle_and_close(tmp_path):
    db_file = str(tmp_path / "lifecycle.db")
    db = DatabaseManager(db_file)
    conn1 = db.get_connection()
    assert conn1 is not None

    # Closing resets local connection
    db.close()
    conn2 = db.get_connection()
    assert conn2 is not None
    assert conn2 is not conn1
    db.close()


def test_source_records_unique_index_enforced(tmp_path):
    """Verify that source_records enforces unique (source_identifier, row_index)."""
    db_file = str(tmp_path / "unique_srec.db")
    db = DatabaseManager(db_file)
    MigrationRunner(db).apply_pending()

    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO source_records (id, source_type, source_identifier, row_index, raw_data_json, checksum, last_synced_at, created_at, updated_at)
            VALUES ('s1', 'LOCAL_XLSX', 'sheet_1', 2, '{}', 'chk1', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
            """
        )

    # Attempting to insert duplicate source_identifier + row_index must raise IntegrityError
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO source_records (id, source_type, source_identifier, row_index, raw_data_json, checksum, last_synced_at, created_at, updated_at)
                VALUES ('s2', 'LOCAL_XLSX', 'sheet_1', 2, '{}', 'chk2', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
                """
            )

