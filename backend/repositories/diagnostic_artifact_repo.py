"""Data access repository for Diagnostic Artifact records."""

import os
from pathlib import Path
from typing import Optional, List
from backend.repositories.base import BaseRepository
from backend.domain.models import DiagnosticArtifact, utc_now_iso


class DiagnosticArtifactRepository(BaseRepository):
    """Repository managing persistent diagnostic records, metadata, and cleanup."""

    def create(self, artifact: DiagnosticArtifact) -> DiagnosticArtifact:
        """Persist a new diagnostic artifact record."""
        query = """
            INSERT INTO diagnostic_artifacts (
                id, task_id, worker_id, session_id, correlation_id,
                timestamp, artifact_type, file_path, page_url,
                page_title, error_code, reason, retention_until
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        params = (
            artifact.id,
            artifact.task_id,
            artifact.worker_id,
            artifact.session_id,
            artifact.correlation_id,
            artifact.timestamp or utc_now_iso(),
            artifact.artifact_type,
            artifact.file_path,
            artifact.page_url,
            artifact.page_title,
            artifact.error_code,
            artifact.reason,
            artifact.retention_until,
        )
        with self.db.transaction() as conn:
            conn.execute(query, params)
        return artifact

    def get_by_id(self, artifact_id: str) -> Optional[DiagnosticArtifact]:
        """Fetch diagnostic artifact record by ID."""
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT * FROM diagnostic_artifacts WHERE id = ?;", (artifact_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_artifact(row)

    def list_by_task(self, task_id: str) -> List[DiagnosticArtifact]:
        """List all diagnostic artifacts associated with a task."""
        conn = self.db.get_connection()
        cursor = conn.execute(
            "SELECT * FROM diagnostic_artifacts WHERE task_id = ? ORDER BY timestamp DESC;",
            (task_id,),
        )
        return [self._row_to_artifact(r) for r in cursor.fetchall()]

    def list_all(self, limit: int = 100) -> List[DiagnosticArtifact]:
        """List recent diagnostic artifacts."""
        conn = self.db.get_connection()
        cursor = conn.execute(
            "SELECT * FROM diagnostic_artifacts ORDER BY timestamp DESC LIMIT ?;",
            (limit,),
        )
        return [self._row_to_artifact(r) for r in cursor.fetchall()]

    def delete_expired(self, now_iso: Optional[str] = None) -> int:
        """
        Delete all diagnostic records whose retention period has expired.
        Also attempts to unlink their associated filesystem artifacts.
        """
        cutoff = now_iso or utc_now_iso()
        conn = self.db.get_connection()
        cursor = conn.execute(
            "SELECT id, file_path FROM diagnostic_artifacts WHERE retention_until IS NOT NULL AND retention_until < ?;",
            (cutoff,),
        )
        expired_rows = cursor.fetchall()
        if not expired_rows:
            return 0

        # Remove underlying files
        for row in expired_rows:
            fp = row["file_path"]
            if fp and os.path.exists(fp):
                try:
                    os.remove(fp)
                except OSError:
                    pass

        # Remove database records
        with self.db.transaction() as t_conn:
            c = t_conn.execute(
                "DELETE FROM diagnostic_artifacts WHERE retention_until IS NOT NULL AND retention_until < ?;",
                (cutoff,),
            )
            return c.rowcount

    def _row_to_artifact(self, row: dict) -> DiagnosticArtifact:
        return DiagnosticArtifact(
            id=row["id"],
            task_id=row["task_id"],
            worker_id=row["worker_id"],
            session_id=row["session_id"],
            correlation_id=row["correlation_id"],
            timestamp=row["timestamp"],
            artifact_type=row["artifact_type"],
            file_path=row["file_path"],
            page_url=row["page_url"],
            page_title=row["page_title"],
            error_code=row["error_code"],
            reason=row["reason"],
            retention_until=row["retention_until"],
        )
