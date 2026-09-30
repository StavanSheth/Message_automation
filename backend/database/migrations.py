"""Deterministic SQLite migration runner."""

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple

from backend.database.manager import DatabaseManager


class MigrationRunner:
    """Discovers, tracks, and deterministically applies database migrations."""

    def __init__(self, db: DatabaseManager, migrations_dir: str = "database/migrations"):
        self.db = db
        self.migrations_dir = Path(migrations_dir)

    def _init_tracking_table(self) -> None:
        """Ensure schema_migrations table exists."""
        with self.db.transaction() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
            """)

    def get_applied_migrations(self) -> List[str]:
        """Return list of applied migration versions sorted."""
        self._init_tracking_table()
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT version FROM schema_migrations ORDER BY version ASC;")
        return [row[0] for row in cursor.fetchall()]

    def get_pending_migrations(self) -> List[Path]:
        """Return list of unapplied migration SQL file paths sorted."""
        if not self.migrations_dir.exists():
            return []

        applied = set(self.get_applied_migrations())
        pending = []

        for entry in sorted(self.migrations_dir.glob("*.sql")):
            version = entry.name
            if version not in applied:
                pending.append(entry)

        return pending

    def apply_pending(self) -> List[str]:
        """Apply all pending migrations in alphabetical order. Returns list of applied versions."""
        self._init_tracking_table()
        pending = self.get_pending_migrations()
        applied_now: List[str] = []

        for migration_file in pending:
            version = migration_file.name
            with open(migration_file, "r", encoding="utf-8") as f:
                sql_content = f.read()

            with self.db.transaction() as conn:
                conn.executescript(sql_content)
                now_iso = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?);",
                    (version, now_iso),
                )
                applied_now.append(version)

        return applied_now

    def verify_schema(self) -> bool:
        """Verify that all core tables exist in the database."""
        required_tables = {
            "schema_migrations",
            "contacts",
            "source_records",
            "tasks",
            "messages",
            "followups",
            "verification_results",
            "automation_runs",
            "workers",
            "browser_sessions",
            "events",
            "errors",
            "sync_runs",
            "settings",
        }
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
        existing_tables = {row[0] for row in cursor.fetchall()}
        return required_tables.issubset(existing_tables)
