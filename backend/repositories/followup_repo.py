"""Followup repository."""

import sqlite3
from typing import Optional, List
from backend.repositories.base import BaseRepository
from backend.domain.models import Followup, utc_now_iso
from backend.domain.enums import FollowupStatus


class FollowupRepository(BaseRepository):
    """Data access repository for Followup entities."""

    def create(self, followup: Followup) -> Followup:
        query = """
            INSERT INTO followups (
                id, contact_id, sequence, message, delay_seconds,
                scheduled_at, status, sent_at, cancelled_at, cancel_reason,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        params = (
            followup.id,
            followup.contact_id,
            followup.sequence,
            followup.message,
            followup.delay_seconds,
            followup.scheduled_at,
            followup.status.value if isinstance(followup.status, FollowupStatus) else followup.status,
            followup.sent_at,
            followup.cancelled_at,
            followup.cancel_reason,
            followup.created_at,
            followup.updated_at,
        )
        with self.db.transaction() as conn:
            conn.execute(query, params)
        return followup

    def get_by_id(self, followup_id: str) -> Optional[Followup]:
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT * FROM followups WHERE id = ?;", (followup_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_followup(row)

    def get_by_contact_and_sequence(self, contact_id: str, sequence: int) -> Optional[Followup]:
        conn = self.db.get_connection()
        cursor = conn.execute(
            "SELECT * FROM followups WHERE contact_id = ? AND sequence = ?;",
            (contact_id, sequence),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_followup(row)

    def list_by_contact(self, contact_id: str) -> List[Followup]:
        conn = self.db.get_connection()
        cursor = conn.execute(
            "SELECT * FROM followups WHERE contact_id = ? ORDER BY sequence ASC;",
            (contact_id,),
        )
        return [self._row_to_followup(row) for row in cursor.fetchall()]

    def cancel_pending_for_contact(self, contact_id: str, cancel_reason: str = "REPLIED") -> int:
        """
        Cancel any pending or scheduled follow-ups for a contact (e.g. when Replied = YES).
        Returns number of followups cancelled.
        """
        now_iso = utc_now_iso()
        query = """
            UPDATE followups SET
                status = 'CANCELLED',
                cancelled_at = ?,
                cancel_reason = ?,
                updated_at = ?
            WHERE contact_id = ?
              AND status IN ('PENDING', 'SCHEDULED', 'DUE');
        """
        with self.db.transaction() as conn:
            cursor = conn.execute(query, (now_iso, cancel_reason, now_iso, contact_id))
            return cursor.rowcount

    def cancel(self, followup_id: str, reason: str = "CANCELLED") -> bool:
        """Cancel a specific followup record."""
        return self.update_status(
            followup_id=followup_id,
            status=FollowupStatus.CANCELLED,
            cancelled_at=utc_now_iso(),
            cancel_reason=reason,
        )

    def update_status(
        self,
        followup_id: str,
        status: FollowupStatus,
        sent_at: Optional[str] = None,
        cancelled_at: Optional[str] = None,
        cancel_reason: Optional[str] = None,
    ) -> bool:
        now_iso = utc_now_iso()
        status_val = status.value if isinstance(status, FollowupStatus) else status

        query = """
            UPDATE followups SET
                status = ?,
                sent_at = COALESCE(?, sent_at),
                cancelled_at = COALESCE(?, cancelled_at),
                cancel_reason = COALESCE(?, cancel_reason),
                updated_at = ?
            WHERE id = ?;
        """
        with self.db.transaction() as conn:
            cursor = conn.execute(
                query, (status_val, sent_at, cancelled_at, cancel_reason, now_iso, followup_id)
            )
            return cursor.rowcount > 0

    def claim_for_materialization(self, followup_id: str) -> bool:
        """
        Atomically transition followup from SCHEDULED to DUE for task materialization.
        Prevents race conditions across concurrent scheduler ticks.
        """
        now_iso = utc_now_iso()
        query = """
            UPDATE followups SET
                status = 'DUE',
                updated_at = ?
            WHERE id = ? AND status = 'SCHEDULED';
        """
        with self.db.transaction() as conn:
            cursor = conn.execute(query, (now_iso, followup_id))
            return cursor.rowcount > 0

    def schedule(self, followup_id: str, scheduled_at_iso: str) -> bool:
        """Schedule a pending followup with an explicit execution time."""
        now_iso = utc_now_iso()
        query = """
            UPDATE followups SET
                status = 'SCHEDULED',
                scheduled_at = ?,
                updated_at = ?
            WHERE id = ? AND status = 'PENDING';
        """
        with self.db.transaction() as conn:
            cursor = conn.execute(query, (scheduled_at_iso, now_iso, followup_id))
            return cursor.rowcount > 0

    def list_due(self, current_time_iso: Optional[str] = None) -> List[Followup]:
        """List followups that are scheduled and now due for processing."""
        if current_time_iso is None:
            current_time_iso = utc_now_iso()
        conn = self.db.get_connection()
        query = """
            SELECT * FROM followups
            WHERE status = 'SCHEDULED'
              AND scheduled_at <= ?
            ORDER BY scheduled_at ASC;
        """
        cursor = conn.execute(query, (current_time_iso,))
        return [self._row_to_followup(row) for row in cursor.fetchall()]

    def _row_to_followup(self, row: sqlite3.Row) -> Followup:
        return Followup(
            id=row["id"],
            contact_id=row["contact_id"],
            sequence=row["sequence"],
            message=row["message"],
            delay_seconds=row["delay_seconds"],
            scheduled_at=row["scheduled_at"],
            status=FollowupStatus(row["status"]),
            sent_at=row["sent_at"],
            cancelled_at=row["cancelled_at"],
            cancel_reason=row["cancel_reason"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
