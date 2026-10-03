"""Error repository for structured error tracking."""

import sqlite3
from typing import Optional, List
from backend.repositories.base import BaseRepository
from backend.domain.models import ErrorRecord, utc_now_iso
from backend.domain.enums import ErrorCode, ErrorSeverity


class ErrorRepository(BaseRepository):
    """Data access repository for structured automation errors."""

    def record(self, error: ErrorRecord) -> ErrorRecord:
        query = """
            INSERT INTO errors (
                id, task_id, code, message, severity,
                retryable, attempt, created_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        code_val = error.code.value if isinstance(error.code, ErrorCode) else error.code
        sev_val = error.severity.value if isinstance(error.severity, ErrorSeverity) else error.severity

        params = (
            error.id,
            error.task_id,
            code_val,
            error.message,
            sev_val,
            1 if error.retryable else 0,
            error.attempt,
            error.created_at,
            error.resolved_at,
        )
        with self.db.transaction() as conn:
            conn.execute(query, params)
        return error

    def get_by_id(self, error_id: str) -> Optional[ErrorRecord]:
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT * FROM errors WHERE id = ?;", (error_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_error(row)

    def list_by_task(self, task_id: str) -> List[ErrorRecord]:
        conn = self.db.get_connection()
        cursor = conn.execute(
            "SELECT * FROM errors WHERE task_id = ? ORDER BY created_at DESC;",
            (task_id,),
        )
        return [self._row_to_error(row) for row in cursor.fetchall()]

    def resolve(self, error_id: str, resolved_at: Optional[str] = None) -> bool:
        if resolved_at is None:
            resolved_at = utc_now_iso()
        query = "UPDATE errors SET resolved_at = ? WHERE id = ?;"
        with self.db.transaction() as conn:
            cursor = conn.execute(query, (resolved_at, error_id))
            return cursor.rowcount > 0

    def list_recent(self, limit: int = 10) -> List[ErrorRecord]:
        conn = self.db.get_connection()
        cursor = conn.execute(
            "SELECT * FROM errors ORDER BY created_at DESC LIMIT ?;",
            (limit,),
        )
        return [self._row_to_error(row) for row in cursor.fetchall()]

    def _row_to_error(self, row: sqlite3.Row) -> ErrorRecord:
        return ErrorRecord(
            id=row["id"],
            task_id=row["task_id"],
            code=ErrorCode(row["code"]),
            message=row["message"],
            severity=ErrorSeverity(row["severity"]),
            retryable=bool(row["retryable"]),
            attempt=row["attempt"],
            created_at=row["created_at"],
            resolved_at=row["resolved_at"],
        )
