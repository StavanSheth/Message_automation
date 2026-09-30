"""Contact repository."""

import sqlite3
from typing import Optional, List
from backend.repositories.base import BaseRepository
from backend.domain.models import Contact, utc_now_iso
from backend.domain.enums import RepliedStatus, RepliedSource


class ContactRepository(BaseRepository):
    """Data access repository for Contact entities."""

    def create(self, contact: Contact) -> Contact:
        """Insert a new contact."""
        query = """
            INSERT INTO contacts (
                id, source_record_id, name, instagram_url, username,
                expected_followers, notes, replied_status, replied_source,
                replied_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        params = (
            contact.id,
            contact.source_record_id,
            contact.name,
            contact.instagram_url,
            contact.username,
            contact.expected_followers,
            contact.notes,
            contact.replied_status.value if isinstance(contact.replied_status, RepliedStatus) else contact.replied_status,
            contact.replied_source.value if isinstance(contact.replied_source, RepliedSource) else contact.replied_source,
            contact.replied_at,
            contact.created_at,
            contact.updated_at,
        )
        with self.db.transaction() as conn:
            conn.execute(query, params)
        return contact

    def get_by_id(self, contact_id: str) -> Optional[Contact]:
        """Fetch a contact by its unique ID."""
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT * FROM contacts WHERE id = ?;", (contact_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_contact(row)

    def get_by_instagram_url(self, url: str) -> Optional[Contact]:
        """Fetch a contact by Instagram profile URL."""
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT * FROM contacts WHERE instagram_url = ?;", (url,))
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_contact(row)

    def update(self, contact: Contact) -> Contact:
        """Update contact fields."""
        contact.updated_at = utc_now_iso()
        query = """
            UPDATE contacts SET
                source_record_id = ?,
                name = ?,
                instagram_url = ?,
                username = ?,
                expected_followers = ?,
                notes = ?,
                replied_status = ?,
                replied_source = ?,
                replied_at = ?,
                updated_at = ?
            WHERE id = ?;
        """
        params = (
            contact.source_record_id,
            contact.name,
            contact.instagram_url,
            contact.username,
            contact.expected_followers,
            contact.notes,
            contact.replied_status.value if isinstance(contact.replied_status, RepliedStatus) else contact.replied_status,
            contact.replied_source.value if isinstance(contact.replied_source, RepliedSource) else contact.replied_source,
            contact.replied_at,
            contact.updated_at,
            contact.id,
        )
        with self.db.transaction() as conn:
            conn.execute(query, params)
        return contact

    def update_replied_status(
        self,
        contact_id: str,
        replied_status: RepliedStatus,
        source: RepliedSource = RepliedSource.MANUAL,
        replied_at: Optional[str] = None,
    ) -> bool:
        """Update replied status (UNKNOWN, YES, NO)."""
        now_iso = utc_now_iso()
        if replied_status == RepliedStatus.YES and replied_at is None:
            replied_at = now_iso

        status_val = replied_status.value if isinstance(replied_status, RepliedStatus) else replied_status
        source_val = source.value if isinstance(source, RepliedSource) else source

        query = """
            UPDATE contacts SET
                replied_status = ?,
                replied_source = ?,
                replied_at = ?,
                updated_at = ?
            WHERE id = ?;
        """
        with self.db.transaction() as conn:
            cursor = conn.execute(query, (status_val, source_val, replied_at, now_iso, contact_id))
            return cursor.rowcount > 0

    def list_all(self, limit: int = 100, offset: int = 0) -> List[Contact]:
        """List contacts paginated."""
        conn = self.db.get_connection()
        cursor = conn.execute(
            "SELECT * FROM contacts ORDER BY created_at DESC LIMIT ? OFFSET ?;",
            (limit, offset),
        )
        return [self._row_to_contact(row) for row in cursor.fetchall()]

    def count(self) -> int:
        """Count total contacts."""
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT COUNT(*) FROM contacts;")
        return cursor.fetchone()[0]

    def _row_to_contact(self, row: sqlite3.Row) -> Contact:
        return Contact(
            id=row["id"],
            source_record_id=row["source_record_id"],
            name=row["name"],
            instagram_url=row["instagram_url"],
            username=row["username"],
            expected_followers=row["expected_followers"],
            notes=row["notes"],
            replied_status=RepliedStatus(row["replied_status"]),
            replied_source=RepliedSource(row["replied_source"]),
            replied_at=row["replied_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
