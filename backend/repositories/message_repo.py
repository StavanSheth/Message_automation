"""Message repository."""

import sqlite3
from typing import Optional, List
from backend.repositories.base import BaseRepository
from backend.domain.models import Message, utc_now_iso
from backend.domain.enums import MessageState


class MessageRepository(BaseRepository):
    """Data access repository for Message entities."""

    def create(self, message: Message) -> Message:
        query = """
            INSERT INTO messages (
                id, contact_id, task_id, sequence, body,
                status, attempted_at, confirmed_at, result_code,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        params = (
            message.id,
            message.contact_id,
            message.task_id,
            message.sequence,
            message.body,
            message.status.value if isinstance(message.status, MessageState) else message.status,
            message.attempted_at,
            message.confirmed_at,
            message.result_code,
            message.created_at,
            message.updated_at,
        )
        with self.db.transaction() as conn:
            conn.execute(query, params)
        return message

    def get_by_id(self, message_id: str) -> Optional[Message]:
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT * FROM messages WHERE id = ?;", (message_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_message(row)

    def get_by_task_id(self, task_id: str) -> Optional[Message]:
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT * FROM messages WHERE task_id = ?;", (task_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_message(row)

    def update_status(
        self,
        message_id: str,
        status: MessageState,
        attempted_at: Optional[str] = None,
        confirmed_at: Optional[str] = None,
        result_code: Optional[str] = None,
    ) -> bool:
        now_iso = utc_now_iso()
        status_val = status.value if isinstance(status, MessageState) else status

        query = """
            UPDATE messages SET
                status = ?,
                attempted_at = COALESCE(?, attempted_at),
                confirmed_at = COALESCE(?, confirmed_at),
                result_code = COALESCE(?, result_code),
                updated_at = ?
            WHERE id = ?;
        """
        with self.db.transaction() as conn:
            cursor = conn.execute(
                query, (status_val, attempted_at, confirmed_at, result_code, now_iso, message_id)
            )
            return cursor.rowcount > 0

    def list_by_contact(self, contact_id: str) -> List[Message]:
        conn = self.db.get_connection()
        cursor = conn.execute(
            "SELECT * FROM messages WHERE contact_id = ? ORDER BY sequence ASC, created_at ASC;",
            (contact_id,),
        )
        return [self._row_to_message(row) for row in cursor.fetchall()]

    def has_confirmed_sent_message(self, task_id: str) -> bool:
        """Check if this task already has a confirmed SENT message in the database."""
        conn = self.db.get_connection()
        cursor = conn.execute(
            "SELECT id FROM messages WHERE task_id = ? AND status = 'SENT' LIMIT 1;",
            (task_id,),
        )
        return cursor.fetchone() is not None

    def is_in_reconciliation(self, task_id: str) -> bool:
        """Check if any message for this task is in RECONCILIATION status."""
        conn = self.db.get_connection()
        cursor = conn.execute(
            "SELECT id FROM messages WHERE task_id = ? AND status = 'RECONCILIATION' LIMIT 1;",
            (task_id,),
        )
        return cursor.fetchone() is not None

    @staticmethod
    def calculate_message_hash(body: str) -> str:
        """Compute deterministic SHA-256 hash of message body for deduplication."""
        import hashlib
        return hashlib.sha256(body.strip().encode("utf-8")).hexdigest()

    def _row_to_message(self, row: sqlite3.Row) -> Message:
        return Message(
            id=row["id"],
            contact_id=row["contact_id"],
            task_id=row["task_id"],
            sequence=row["sequence"],
            body=row["body"],
            status=MessageState(row["status"]),
            attempted_at=row["attempted_at"],
            confirmed_at=row["confirmed_at"],
            result_code=row["result_code"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
