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

    def resolve(self, item_id: str, status: str = "RESOLVED") -> bool:
        """Mark a manual review item as resolved or dismissed."""
        now_iso = utc_now_iso()
        query = """
            UPDATE manual_reviews SET
                status = ?,
                resolved_at = ?
            WHERE id = ?;
        """
        with self.db.transaction() as conn:
            cursor = conn.execute(query, (status, now_iso, item_id))
            return cursor.rowcount > 0

    def count_by_status(self) -> Dict[str, int]:
        """Count manual review items grouped by status."""
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT status, COUNT(*) FROM manual_reviews GROUP BY status;")
        return {row[0]: row[1] for row in cursor.fetchall()}

    def _row_to_item(self, row: sqlite3.Row) -> ManualReviewItem:
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
        )
