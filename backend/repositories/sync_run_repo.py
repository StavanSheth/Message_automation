"""SyncRun repository."""

import sqlite3
from typing import Optional, List
from backend.repositories.base import BaseRepository
from backend.domain.models import SyncRun, utc_now_iso
from backend.domain.enums import SyncStatus, SourceType


class SyncRunRepository(BaseRepository):
    """Data access repository for tracking source sync executions."""

    def create(self, sync_run: SyncRun) -> SyncRun:
        query = """
            INSERT INTO sync_runs (
                id, sync_code, source_type, source_identifier,
                started_at, completed_at, status, records_read,
                records_written, conflicts, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        stype_val = sync_run.source_type.value if isinstance(sync_run.source_type, SourceType) else sync_run.source_type
        status_val = sync_run.status.value if isinstance(sync_run.status, SyncStatus) else sync_run.status

        params = (
            sync_run.id,
            sync_run.sync_code,
            stype_val,
            sync_run.source_identifier,
            sync_run.started_at,
            sync_run.completed_at,
            status_val,
            sync_run.records_read,
            sync_run.records_written,
            sync_run.conflicts,
            sync_run.error,
        )
        with self.db.transaction() as conn:
            conn.execute(query, params)
        return sync_run

    def update(self, sync_run: SyncRun) -> SyncRun:
        status_val = sync_run.status.value if isinstance(sync_run.status, SyncStatus) else sync_run.status
        query = """
            UPDATE sync_runs SET
                completed_at = ?,
                status = ?,
                records_read = ?,
                records_written = ?,
                conflicts = ?,
                error = ?
            WHERE id = ?;
        """
        params = (
            sync_run.completed_at,
            status_val,
            sync_run.records_read,
            sync_run.records_written,
            sync_run.conflicts,
            sync_run.error,
            sync_run.id,
        )
        with self.db.transaction() as conn:
            conn.execute(query, params)
        return sync_run

    def get_by_id(self, sync_id: str) -> Optional[SyncRun]:
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT * FROM sync_runs WHERE id = ?;", (sync_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_sync_run(row)

    def get_latest(self, source_identifier: str) -> Optional[SyncRun]:
        conn = self.db.get_connection()
        cursor = conn.execute(
            "SELECT * FROM sync_runs WHERE source_identifier = ? ORDER BY started_at DESC LIMIT 1;",
            (source_identifier,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_sync_run(row)

    def list_by_source(self, source_identifier: str, limit: int = 50) -> List[SyncRun]:
        conn = self.db.get_connection()
        cursor = conn.execute(
            "SELECT * FROM sync_runs WHERE source_identifier = ? ORDER BY started_at DESC LIMIT ?;",
            (source_identifier, limit),
        )
        return [self._row_to_sync_run(row) for row in cursor.fetchall()]

    def _row_to_sync_run(self, row: sqlite3.Row) -> SyncRun:
        return SyncRun(
            id=row["id"],
            sync_code=row["sync_code"],
            source_type=SourceType(row["source_type"]),
            source_identifier=row["source_identifier"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            status=SyncStatus(row["status"]),
            records_read=row["records_read"],
            records_written=row["records_written"],
            conflicts=row["conflicts"],
            error=row["error"],
        )
