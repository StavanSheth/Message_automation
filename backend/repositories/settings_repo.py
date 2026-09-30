"""Settings repository for persisting system settings in SQLite."""

import sqlite3
from typing import Optional, Dict
from backend.repositories.base import BaseRepository
from backend.domain.models import utc_now_iso


class SettingsRepository(BaseRepository):
    """Data access repository for application key-value settings."""

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT value FROM settings WHERE key = ?;", (key,))
        row = cursor.fetchone()
        if not row:
            return default
        return row[0]

    def set(self, key: str, value: str) -> None:
        now_iso = utc_now_iso()
        query = """
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at;
        """
        with self.db.transaction() as conn:
            conn.execute(query, (key, value, now_iso))

    def get_all(self) -> Dict[str, str]:
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT key, value FROM settings;")
        return {row[0]: row[1] for row in cursor.fetchall()}

    def delete(self, key: str) -> bool:
        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM settings WHERE key = ?;", (key,))
            return cursor.rowcount > 0
