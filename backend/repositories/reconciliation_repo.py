"""Reconciliation repository for tracking ambiguous automation states and resolutions."""

import sqlite3
from typing import Optional, List, Dict
from backend.repositories.base import BaseRepository
from backend.domain.models import ReconciliationRecord, utc_now_iso
from backend.domain.enums import ReconciliationState, ReconciliationResolution


class ReconciliationRepository(BaseRepository):
    """Data access repository for Reconciliation records."""

    def create(self, record: ReconciliationRecord) -> ReconciliationRecord:
        """Insert a new reconciliation record."""
        now_iso = utc_now_iso()
        record.created_at = record.created_at or now_iso
        record.updated_at = record.updated_at or now_iso

        query = """
            INSERT INTO reconciliations (
                id, task_id, message_id, worker_id, session_id,
                state, reason, observed_state, resolution,
                resolution_source, created_at, updated_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        params = (
            record.id,
            record.task_id,
            record.message_id,
            record.worker_id,
            record.session_id,
            record.state,
            record.reason,
            record.observed_state,
            record.resolution,
            record.resolution_source,
            record.created_at,
            record.updated_at,
            record.resolved_at,
        )
        with self.db.transaction() as conn:
            conn.execute(query, params)
        return record

    def get_by_id(self, record_id: str) -> Optional[ReconciliationRecord]:
        """Fetch a reconciliation record by ID."""
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT * FROM reconciliations WHERE id = ?;", (record_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_record(row)

    def get_by_task_id(self, task_id: str) -> Optional[ReconciliationRecord]:
        """Fetch the latest reconciliation record for a task."""
        conn = self.db.get_connection()
        cursor = conn.execute(
            "SELECT * FROM reconciliations WHERE task_id = ? ORDER BY created_at DESC LIMIT 1;",
            (task_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_record(row)

    def list_pending(self) -> List[ReconciliationRecord]:
        """List all pending or in-progress reconciliations."""
        conn = self.db.get_connection()
        cursor = conn.execute(
            "SELECT * FROM reconciliations WHERE state IN ('PENDING', 'IN_PROGRESS') ORDER BY created_at ASC;"
        )
        return [self._row_to_record(row) for row in cursor.fetchall()]

    def update_resolution(
        self,
        record_id: str,
        state: str,
        resolution: str,
        resolution_source: str,
        observed_state: Optional[str] = None,
    ) -> bool:
        """Update resolution on a reconciliation record."""
        now_iso = utc_now_iso()
        query = """
            UPDATE reconciliations SET
                state = ?,
                resolution = ?,
                resolution_source = ?,
                observed_state = COALESCE(?, observed_state),
                resolved_at = ?,
                updated_at = ?
            WHERE id = ?;
        """
        with self.db.transaction() as conn:
            cursor = conn.execute(
                query, (state, resolution, resolution_source, observed_state, now_iso, now_iso, record_id)
            )
            return cursor.rowcount > 0

    def count_by_state(self) -> Dict[str, int]:
        """Count reconciliation records grouped by state."""
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT state, COUNT(*) FROM reconciliations GROUP BY state;")
        return {row[0]: row[1] for row in cursor.fetchall()}

    def _row_to_record(self, row: sqlite3.Row) -> ReconciliationRecord:
        return ReconciliationRecord(
            id=row["id"],
            task_id=row["task_id"],
            message_id=row["message_id"],
            worker_id=row["worker_id"],
            session_id=row["session_id"],
            state=row["state"],
            reason=row["reason"],
            observed_state=row["observed_state"],
            resolution=row["resolution"],
            resolution_source=row["resolution_source"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            resolved_at=row["resolved_at"],
        )
