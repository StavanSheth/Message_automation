"""Authoritative repository for durable execution identities ensuring cross-worker/cross-process idempotency."""

import sqlite3
from typing import Optional, List
from backend.repositories.base import BaseRepository
from backend.domain.models import ExecutionIdentity, utc_now_iso


class ExecutionIdentityRepository(BaseRepository):
    """Data access repository for durable execution identities."""

    def create(self, identity: ExecutionIdentity) -> ExecutionIdentity:
        """Insert a new execution identity record. Fails on duplicate execution_key."""
        now_iso = utc_now_iso()
        identity.created_at = identity.created_at or now_iso
        identity.started_at = identity.started_at or now_iso

        query = """
            INSERT INTO execution_identities (
                execution_key, task_id, message_id, contact_id,
                message_hash, attempt, worker_id, session_id,
                correlation_id, state, outcome, created_at,
                started_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        params = (
            identity.execution_key,
            identity.task_id,
            identity.message_id,
            identity.contact_id,
            identity.message_hash,
            identity.attempt,
            identity.worker_id,
            identity.session_id,
            identity.correlation_id,
            identity.state,
            identity.outcome,
            identity.created_at,
            identity.started_at,
            identity.completed_at,
        )
        with self.db.transaction() as conn:
            conn.execute(query, params)
        return identity

    def get(self, execution_key: str) -> Optional[ExecutionIdentity]:
        """Fetch execution identity by key."""
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT * FROM execution_identities WHERE execution_key = ?;", (execution_key,))
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_identity(row)

    def get_by_task_id(self, task_id: str) -> Optional[ExecutionIdentity]:
        """Fetch execution identity for a task."""
        conn = self.db.get_connection()
        cursor = conn.execute(
            "SELECT * FROM execution_identities WHERE task_id = ? ORDER BY created_at DESC LIMIT 1;",
            (task_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_identity(row)

    def update_state(
        self,
        execution_key: str,
        state: str,
        outcome: Optional[str] = None,
        completed_at: Optional[str] = None,
    ) -> bool:
        """Update state and outcome of an execution identity."""
        if state in ("SENT", "FAILED", "MANUAL_REVIEW") and not completed_at:
            completed_at = utc_now_iso()

        query = """
            UPDATE execution_identities
            SET state = ?, outcome = ?, completed_at = COALESCE(?, completed_at)
            WHERE execution_key = ?;
        """
        with self.db.transaction() as conn:
            cursor = conn.execute(query, (state, outcome, completed_at, execution_key))
            return cursor.rowcount > 0

    def delete(self, execution_key: str) -> bool:
        """Delete an execution key (e.g. on clean completion if in-flight tracking only)."""
        query = "DELETE FROM execution_identities WHERE execution_key = ?;"
        with self.db.transaction() as conn:
            cursor = conn.execute(query, (execution_key,))
            return cursor.rowcount > 0

    def _row_to_identity(self, row: sqlite3.Row) -> ExecutionIdentity:
        return ExecutionIdentity(
            execution_key=row["execution_key"],
            task_id=row["task_id"],
            message_id=row["message_id"],
            contact_id=row["contact_id"],
            message_hash=row["message_hash"],
            attempt=row["attempt"],
            worker_id=row["worker_id"],
            session_id=row["session_id"],
            correlation_id=row["correlation_id"],
            state=row["state"],
            outcome=row["outcome"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )
