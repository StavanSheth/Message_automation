"""Database connection and transaction manager for SQLite."""

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Generator

from backend.config.settings import get_settings


class DatabaseManager:
    """Manages SQLite connections with WAL mode, foreign keys, and safe transactions."""

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_path = get_settings().database_path
        self.db_path = str(db_path)
        self._local = threading.local()
        self._ensure_parent_directory()

    def _ensure_parent_directory(self) -> None:
        """Create parent directory if database is not in-memory."""
        if self.db_path != ":memory:":
            p = Path(self.db_path)
            p.parent.mkdir(parents=True, exist_ok=True)

    def get_connection(self) -> sqlite3.Connection:
        """Get or create a thread-local SQLite connection."""
        if not hasattr(self._local, "connection") or self._local.connection is None:
            conn = sqlite3.connect(
                self.db_path,
                timeout=10.0,
                detect_types=sqlite3.PARSE_DECLTYPES,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            # Enforce database pragmas
            conn.execute("PRAGMA foreign_keys = ON;")
            if self.db_path != ":memory:":
                conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA busy_timeout = 5000;")
            self._local.connection = conn
        return self._local.connection

    def close(self) -> None:
        """Close thread-local connection if open."""
        if hasattr(self._local, "connection") and self._local.connection is not None:
            try:
                self._local.connection.close()
            except Exception:
                pass
            self._local.connection = None

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager for atomic transaction block."""
        conn = self.get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def execute(self, query: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a parameterized query using the thread connection."""
        conn = self.get_connection()
        return conn.execute(query, params)

    def executemany(self, query: str, params_seq: list) -> sqlite3.Cursor:
        """Execute parameterized query over sequence."""
        conn = self.get_connection()
        return conn.executemany(query, params_seq)
