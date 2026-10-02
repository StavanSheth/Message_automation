"""Authoritative repository for persistent system controls and operational state."""

from typing import Optional, Dict
from backend.repositories.base import BaseRepository
from backend.domain.enums import SystemState
from backend.domain.models import utc_now_iso


class SystemControlRepository(BaseRepository):
    """Data access repository for system_controls table."""

    def get(self, key: str) -> Optional[str]:
        """Fetch string value for a control key."""
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT value FROM system_controls WHERE key = ?;", (key,))
        row = cursor.fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        """Upsert a control key-value pair."""
        now_iso = utc_now_iso()
        query = """
            INSERT INTO system_controls (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at;
        """
        with self.db.transaction() as conn:
            conn.execute(query, (key, value, now_iso))

    def delete(self, key: str) -> bool:
        """Delete a control key."""
        with self.db.transaction() as conn:
            cur = conn.execute("DELETE FROM system_controls WHERE key = ?;", (key,))
            return cur.rowcount > 0

    def get_state(self) -> Optional[SystemState]:
        """Retrieve authoritative persistent system state."""
        val = self.get("system_state")
        if not val:
            return None
        try:
            return SystemState(val)
        except ValueError:
            return None

    def set_state(self, state: SystemState) -> None:
        """Persist system state."""
        self.set("system_state", state.value)
