"""SourceRecord repository."""

import sqlite3
from typing import Optional, List
from backend.repositories.base import BaseRepository
from backend.domain.models import SourceRecord, utc_now_iso
from backend.domain.enums import SourceType


class SourceRecordRepository(BaseRepository):
    """Data access repository for SourceRecord entities representing original spreadsheet rows."""

    def create(self, record: SourceRecord) -> SourceRecord:
        query = """
            INSERT INTO source_records (
                id, source_type, source_identifier, row_index,
                raw_data_json, checksum, last_synced_at, contact_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        params = (
            record.id,
            record.source_type.value if isinstance(record.source_type, SourceType) else record.source_type,
            record.source_identifier,
            record.row_index,
            record.raw_data_json,
            record.checksum,
            record.last_synced_at,
            record.contact_id,
            record.created_at,
            record.updated_at,
        )
        with self.db.transaction() as conn:
            conn.execute(query, params)
        return record

    def get_by_id(self, record_id: str) -> Optional[SourceRecord]:
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT * FROM source_records WHERE id = ?;", (record_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_record(row)

    def get_by_source_and_row(self, source_identifier: str, row_index: int) -> Optional[SourceRecord]:
        conn = self.db.get_connection()
        cursor = conn.execute(
            "SELECT * FROM source_records WHERE source_identifier = ? AND row_index = ?;",
            (source_identifier, row_index),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_record(row)

    def update(self, record: SourceRecord) -> SourceRecord:
        record.updated_at = utc_now_iso()
        query = """
            UPDATE source_records SET
                source_type = ?,
                source_identifier = ?,
                row_index = ?,
                raw_data_json = ?,
                checksum = ?,
                last_synced_at = ?,
                contact_id = ?,
                updated_at = ?
            WHERE id = ?;
        """
        params = (
            record.source_type.value if isinstance(record.source_type, SourceType) else record.source_type,
            record.source_identifier,
            record.row_index,
            record.raw_data_json,
            record.checksum,
            record.last_synced_at,
            record.contact_id,
            record.updated_at,
            record.id,
        )
        with self.db.transaction() as conn:
            conn.execute(query, params)
        return record

    def list_by_source(self, source_identifier: str) -> List[SourceRecord]:
        conn = self.db.get_connection()
        cursor = conn.execute(
            "SELECT * FROM source_records WHERE source_identifier = ? ORDER BY row_index ASC;",
            (source_identifier,),
        )
        return [self._row_to_record(row) for row in cursor.fetchall()]

    def _row_to_record(self, row: sqlite3.Row) -> SourceRecord:
        return SourceRecord(
            id=row["id"],
            source_type=SourceType(row["source_type"]),
            source_identifier=row["source_identifier"],
            row_index=row["row_index"],
            raw_data_json=row["raw_data_json"],
            checksum=row["checksum"],
            last_synced_at=row["last_synced_at"],
            contact_id=row["contact_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
