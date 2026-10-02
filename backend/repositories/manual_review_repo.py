"""Manual review repository for tracking tasks requiring human intervention."""

import sqlite3
from typing import Optional, List, Dict
from backend.repositories.base import BaseRepository
from backend.domain.models import ManualReviewItem, utc_now_iso


class ManualReviewRepository(BaseRepository):
    """Data access repository for ManualReviewItem records."""

    def create(self, item: ManualReviewItem) -> ManualReviewItem:
        """Insert a new manual review item."""
        now_iso = utc_now_iso()
        item.created_at = item.created_at or now_iso

        query = """
            INSERT INTO manual_reviews (
                id, task_id, contact_id, reason, current_state,
                evidence_json, recommended_action, status,
                created_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        params = (
            item.id,
            item.task_id,
            item.contact_id,
            item.reason,
            item.current_state,
            item.evidence_json,
            item.recommended_action,
            item.status,
            item.created_at,
            item.resolved_at,
        )
        with self.db.transaction() as conn:
            conn.execute(query, params)
        return item

    def get_by_id(self, item_id: str) -> Optional[ManualReviewItem]:
        """Fetch a manual review item by ID."""
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT * FROM manual_reviews WHERE id = ?;", (item_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_item(row)

    def get_by_task_id(self, task_id: str) -> Optional[ManualReviewItem]:
        """Fetch manual review item for a specific task."""
        conn = self.db.get_connection()
        cursor = conn.execute(
            "SELECT * FROM manual_reviews WHERE task_id = ? ORDER BY created_at DESC LIMIT 1;",
            (task_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_item(row)

    def list_pending(self) -> List[ManualReviewItem]:
        """List all pending manual review items."""
        conn = self.db.get_connection()
        cursor = conn.execute(
            "SELECT * FROM manual_reviews WHERE status = 'PENDING' ORDER BY created_at ASC;"
        )
        return [self._row_to_item(row) for row in cursor.fetchall()]

    def get_pending_for_task(self, task_id: str) -> Optional[ManualReviewItem]:
        """Fetch pending manual review item for a task if one exists."""
        conn = self.db.get_connection()
        cursor = conn.execute(
            "SELECT * FROM manual_reviews WHERE task_id = ? AND status = 'PENDING' ORDER BY created_at DESC LIMIT 1;",
            (task_id,),
        )
        row = cursor.fetchone()
        return self._row_to_item(row) if row else None

    def resolve(
        self,
        item_id: str,
        status: str = "RESOLVED",
        resolution: Optional[str] = None,
        resolved_by: Optional[str] = None,
        resolution_notes: Optional[str] = None,
    ) -> bool:
        """Mark a manual review item as resolved with explicit resolution details."""
        now_iso = utc_now_iso()
        has_resolution_col = False
        try:
            conn = self.db.get_connection()
            cur = conn.execute("PRAGMA table_info(manual_reviews);")
            cols = {r[1] for r in cur.fetchall()}
            has_resolution_col = "resolution" in cols
        except Exception:
            pass

        if has_resolution_col:
            query = """
                UPDATE manual_reviews SET
                    status = ?,
                    resolved_at = ?,
                    resolution = coalesce(?, resolution),
                    resolved_by = coalesce(?, resolved_by),
                    resolution_notes = coalesce(?, resolution_notes)
                WHERE id = ?;
            """
            params = (status, now_iso, resolution, resolved_by, resolution_notes, item_id)
        else:
            query = "UPDATE manual_reviews SET status = ?, resolved_at = ? WHERE id = ?;"
            params = (status, now_iso, item_id)

        with self.db.transaction() as conn:
            cursor = conn.execute(query, params)
            return cursor.rowcount > 0

    def count_by_status(self) -> Dict[str, int]:
        """Count manual review items grouped by status."""
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT status, COUNT(*) FROM manual_reviews GROUP BY status;")
        return {row[0]: row[1] for row in cursor.fetchall()}

    def _row_to_item(self, row: sqlite3.Row) -> ManualReviewItem:
        cols = row.keys() if hasattr(row, "keys") else []
        return ManualReviewItem(
            id=row["id"],
            task_id=row["task_id"],
            contact_id=row["contact_id"],
            reason=row["reason"],
            current_state=row["current_state"],
            evidence_json=row["evidence_json"],
            recommended_action=row["recommended_action"],
            status=row["status"],
            created_at=row["created_at"],
            resolved_at=row["resolved_at"],
            resolution=row["resolution"] if "resolution" in cols else None,
            resolved_by=row["resolved_by"] if "resolved_by" in cols else None,
            resolution_notes=row["resolution_notes"] if "resolution_notes" in cols else None,
        )

