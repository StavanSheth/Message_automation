"""BrowserSession repository."""

import sqlite3
from typing import Optional, List
from backend.repositories.base import BaseRepository
from backend.domain.models import BrowserSession, utc_now_iso


class BrowserSessionRepository(BaseRepository):
    """Data access repository for browser sessions."""

    def create(self, session: BrowserSession) -> BrowserSession:
        query = """
            INSERT INTO browser_sessions (
                id, worker_id, profile_path, status, started_at, closed_at
            ) VALUES (?, ?, ?, ?, ?, ?);
        """
        params = (
            session.id,
            session.worker_id,
            session.profile_path,
            session.status,
            session.started_at,
            session.closed_at,
        )
        with self.db.transaction() as conn:
            conn.execute(query, params)
        return session

    def update(self, session: BrowserSession) -> BrowserSession:
        query = """
            UPDATE browser_sessions SET
                worker_id = ?,
                profile_path = ?,
                status = ?,
                started_at = ?,
                closed_at = ?
            WHERE id = ?;
        """
        params = (
            session.worker_id,
            session.profile_path,
            session.status,
            session.started_at,
            session.closed_at,
            session.id,
        )
        with self.db.transaction() as conn:
            conn.execute(query, params)
        return session

    def get_by_id(self, session_id: str) -> Optional[BrowserSession]:
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT * FROM browser_sessions WHERE id = ?;", (session_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_session(row)

    def get_by_worker_id(self, worker_id: str) -> List[BrowserSession]:
        conn = self.db.get_connection()
        cursor = conn.execute(
            "SELECT * FROM browser_sessions WHERE worker_id = ? ORDER BY started_at DESC;",
            (worker_id,),
        )
        return [self._row_to_session(row) for row in cursor.fetchall()]

    def list_active(self) -> List[BrowserSession]:
        conn = self.db.get_connection()
        cursor = conn.execute(
            "SELECT * FROM browser_sessions WHERE closed_at IS NULL ORDER BY started_at DESC;"
        )
        return [self._row_to_session(row) for row in cursor.fetchall()]

    def close_session(self, session_id: str, closed_at: Optional[str] = None) -> Optional[BrowserSession]:
        if closed_at is None:
            closed_at = utc_now_iso()
        query = "UPDATE browser_sessions SET status = 'CLOSED', closed_at = ? WHERE id = ?;"
        with self.db.transaction() as conn:
            conn.execute(query, (closed_at, session_id))
        return self.get_by_id(session_id)

    def _row_to_session(self, row: sqlite3.Row) -> BrowserSession:
        return BrowserSession(
            id=row["id"],
            profile_path=row["profile_path"],
            status=row["status"],
            started_at=row["started_at"],
            worker_id=row["worker_id"],
            closed_at=row["closed_at"],
        )
