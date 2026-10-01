"""AutomationRun repository."""

import sqlite3
from typing import Optional, List
from backend.repositories.base import BaseRepository
from backend.domain.models import AutomationRun


class AutomationRunRepository(BaseRepository):
    """Data access repository for automation execution runs."""

    def create(self, run: AutomationRun) -> AutomationRun:
        query = """
            INSERT INTO automation_runs (
                id, run_code, worker_id, task_id, status, started_at, ended_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """
        params = (
            run.id,
            run.run_code,
            run.worker_id,
            run.task_id,
            run.status,
            run.started_at,
            run.ended_at,
            run.metadata_json,
        )
        with self.db.transaction() as conn:
            conn.execute(query, params)
        return run

    def update(self, run: AutomationRun) -> AutomationRun:
        query = """
            UPDATE automation_runs SET
                worker_id = ?,
                task_id = ?,
                status = ?,
                ended_at = ?,
                metadata_json = ?
            WHERE id = ?;
        """
        params = (
            run.worker_id,
            run.task_id,
            run.status,
            run.ended_at,
            run.metadata_json,
            run.id,
        )
        with self.db.transaction() as conn:
            conn.execute(query, params)
        return run

    def get_by_id(self, run_id: str) -> Optional[AutomationRun]:
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT * FROM automation_runs WHERE id = ?;", (run_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_run(row)

    def get_by_run_code(self, run_code: str) -> Optional[AutomationRun]:
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT * FROM automation_runs WHERE run_code = ?;", (run_code,))
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_run(row)

    def list_runs(self, limit: int = 50, status: Optional[str] = None) -> List[AutomationRun]:
        conn = self.db.get_connection()
        if status:
            cursor = conn.execute(
                "SELECT * FROM automation_runs WHERE status = ? ORDER BY started_at DESC LIMIT ?;",
                (status, limit),
            )
        else:
            cursor = conn.execute(
                "SELECT * FROM automation_runs ORDER BY started_at DESC LIMIT ?;",
                (limit,),
            )
        return [self._row_to_run(row) for row in cursor.fetchall()]

    def _row_to_run(self, row: sqlite3.Row) -> AutomationRun:
        return AutomationRun(
            id=row["id"],
            run_code=row["run_code"],
            worker_id=row["worker_id"],
            task_id=row["task_id"],
            status=row["status"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            metadata_json=row["metadata_json"],
        )
