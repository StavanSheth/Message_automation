"""Worker repository."""

import sqlite3
from typing import Optional, List
from backend.repositories.base import BaseRepository
from backend.domain.models import WorkerRecord
from backend.domain.enums import WorkerMode, WorkerStatus


class WorkerRepository(BaseRepository):
    """Data access repository for workers."""

    def create(self, worker: WorkerRecord) -> WorkerRecord:
        query = """
            INSERT INTO workers (
                id, worker_code, mode, status, current_task_id, last_heartbeat, metadata_json, account_id, quarantine_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        mode_val = worker.mode.value if isinstance(worker.mode, WorkerMode) else worker.mode
        status_val = worker.status.value if isinstance(worker.status, WorkerStatus) else worker.status
        params = (
            worker.id,
            worker.worker_code,
            mode_val,
            status_val,
            worker.current_task_id,
            worker.last_heartbeat,
            worker.metadata_json,
            getattr(worker, "account_id", None),
            getattr(worker, "quarantine_reason", None),
        )
        with self.db.transaction() as conn:
            conn.execute(query, params)
        return worker

    def update(self, worker: WorkerRecord) -> WorkerRecord:
        query = """
            UPDATE workers SET
                mode = ?,
                status = ?,
                current_task_id = ?,
                last_heartbeat = ?,
                metadata_json = ?,
                account_id = ?,
                quarantine_reason = ?
            WHERE id = ?;
        """
        mode_val = worker.mode.value if isinstance(worker.mode, WorkerMode) else worker.mode
        status_val = worker.status.value if isinstance(worker.status, WorkerStatus) else worker.status
        params = (
            mode_val,
            status_val,
            worker.current_task_id,
            worker.last_heartbeat,
            worker.metadata_json,
            getattr(worker, "account_id", None),
            getattr(worker, "quarantine_reason", None),
            worker.id,
        )
        with self.db.transaction() as conn:
            conn.execute(query, params)
        return worker

    def get_by_id(self, worker_id: str) -> Optional[WorkerRecord]:
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT * FROM workers WHERE id = ?;", (worker_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_worker(row)

    def get_by_code(self, worker_code: str) -> Optional[WorkerRecord]:
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT * FROM workers WHERE worker_code = ?;", (worker_code,))
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_worker(row)

    def list_workers(self, status: Optional[WorkerStatus] = None) -> List[WorkerRecord]:
        conn = self.db.get_connection()
        if status:
            status_val = status.value if isinstance(status, WorkerStatus) else status
            cursor = conn.execute("SELECT * FROM workers WHERE status = ?;", (status_val,))
        else:
            cursor = conn.execute("SELECT * FROM workers;")
        return [self._row_to_worker(row) for row in cursor.fetchall()]

    def list_all(self) -> List[WorkerRecord]:
        """Alias for list_workers()."""
        return self.list_workers()

    def delete(self, worker_id: str) -> bool:
        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM workers WHERE id = ?;", (worker_id,))
            return cursor.rowcount > 0

    def _row_to_worker(self, row: sqlite3.Row) -> WorkerRecord:
        acc_id = row["account_id"] if "account_id" in row.keys() else None
        q_reason = row["quarantine_reason"] if "quarantine_reason" in row.keys() else None
        return WorkerRecord(
            id=row["id"],
            worker_code=row["worker_code"],
            mode=WorkerMode(row["mode"]),
            status=WorkerStatus(row["status"]),
            current_task_id=row["current_task_id"],
            last_heartbeat=row["last_heartbeat"],
            metadata_json=row["metadata_json"],
            account_id=acc_id,
            quarantine_reason=q_reason,
        )
