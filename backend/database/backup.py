"""Database backup and recovery service ensuring durable local disaster recovery and schema verification."""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.events.logger import get_logger

logger = get_logger("database_backup")


class DatabaseBackupService:
    """
    Manages local transactional SQLite backups, schema verification, and restore operations.
    Follows Section 17:
    - DB backup using transactional SQLite backup API (WAL-safe)
    - Backup integrity validation (PRAGMA integrity_check + foreign_key_check)
    - Migration & schema verification via MigrationRunner
    - Transactional restore to target/temporary DB
    """

    def __init__(self, db: DatabaseManager, default_backup_dir: Optional[str] = None):
        self.db = db
        if default_backup_dir:
            self.backup_dir = Path(default_backup_dir)
        else:
            p = Path(self.db.db_path)
            self.backup_dir = p.parent / "backups" if self.db.db_path != ":memory:" else Path("data/backups")
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def create_backup(self, target_path: Optional[str] = None) -> str:
        """
        Create a consistent online backup of the SQLite database.
        Returns the absolute path to the backup file.
        """
        if target_path:
            dest_path = Path(target_path)
        else:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            dest_path = self.backup_dir / f"backup_{timestamp}.db"

        dest_path.parent.mkdir(parents=True, exist_ok=True)

        # Source connection
        src_conn = self.db.get_connection()
        # Destination connection
        dest_conn = sqlite3.connect(str(dest_path))
        try:
            # SQLite online backup API ensures WAL consistency
            with dest_conn:
                src_conn.backup(dest_conn, pages=-1)
        finally:
            dest_conn.close()

        logger.info(f"Database backup created at {dest_path}")
        return str(dest_path)

    def verify_backup(self, backup_path: str) -> bool:
        """
        Verify the integrity, foreign keys, and schema of a database backup file.
        Returns True if completely valid, False otherwise.
        """
        if not os.path.exists(backup_path):
            logger.error(f"Backup verification failed: file {backup_path} does not exist")
            return False

        try:
            conn = sqlite3.connect(backup_path)
            conn.row_factory = sqlite3.Row

            # 1. PRAGMA integrity_check
            cur = conn.execute("PRAGMA integrity_check;")
            row = cur.fetchone()
            if not row or row[0].lower() != "ok":
                logger.error(f"Backup integrity check failed for {backup_path}: {row}")
                conn.close()
                return False

            # 2. PRAGMA foreign_key_check
            cur = conn.execute("PRAGMA foreign_key_check;")
            fk_violations = cur.fetchall()
            if fk_violations:
                logger.error(f"Backup foreign key violations in {backup_path}: {fk_violations}")
                conn.close()
                return False

            conn.close()

            # 3. Schema verification via MigrationRunner
            backup_db = DatabaseManager(backup_path)
            runner = MigrationRunner(backup_db)
            if not runner.verify_schema():
                logger.error(f"Backup schema verification failed for {backup_path}")
                backup_db.close()
                return False
            backup_db.close()

            logger.info(f"Backup {backup_path} verified successfully")
            return True

        except Exception as e:
            logger.error(f"Error during backup verification: {e}")
            return False

    def restore_backup(self, backup_path: str, destination_path: str) -> bool:
        """
        Restore a backup to destination_path after verifying its integrity.
        Validates the restored database upon completion.
        """
        if not self.verify_backup(backup_path):
            logger.error(f"Cannot restore invalid backup from {backup_path}")
            return False

        dest_file = Path(destination_path)
        dest_file.parent.mkdir(parents=True, exist_ok=True)

        src_conn = sqlite3.connect(backup_path)
        dest_conn = sqlite3.connect(str(dest_file))
        try:
            with dest_conn:
                src_conn.backup(dest_conn, pages=-1)
        finally:
            src_conn.close()
            dest_conn.close()

        # Verify restored destination database
        if not self.verify_backup(str(dest_file)):
            logger.error(f"Verification of restored database failed at {dest_file}")
            return False

        logger.info(f"Database successfully restored from {backup_path} to {destination_path}")
        return True
