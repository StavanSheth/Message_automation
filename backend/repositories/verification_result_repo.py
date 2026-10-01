"""VerificationResult repository."""

import sqlite3
from typing import Optional, List
from backend.repositories.base import BaseRepository
from backend.domain.models import VerificationResult
from backend.domain.enums import VerificationDecision


class VerificationResultRepository(BaseRepository):
    """Data access repository for contact verification results."""

    def create(self, result: VerificationResult) -> VerificationResult:
        query = """
            INSERT INTO verification_results (
                id, contact_id, task_id, confidence, decision, signals_json, ocr_text, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """
        decision_val = (
            result.decision.value
            if isinstance(result.decision, VerificationDecision)
            else result.decision
        )
        params = (
            result.id,
            result.contact_id,
            result.task_id,
            result.confidence,
            decision_val,
            result.signals_json,
            result.ocr_text,
            result.created_at,
        )
        with self.db.transaction() as conn:
            conn.execute(query, params)
        return result

    def get_by_id(self, result_id: str) -> Optional[VerificationResult]:
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT * FROM verification_results WHERE id = ?;", (result_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_result(row)

    def get_by_task_id(self, task_id: str) -> Optional[VerificationResult]:
        conn = self.db.get_connection()
        cursor = conn.execute(
            "SELECT * FROM verification_results WHERE task_id = ? ORDER BY created_at DESC LIMIT 1;",
            (task_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_result(row)

    def list_by_contact_id(self, contact_id: str) -> List[VerificationResult]:
        conn = self.db.get_connection()
        cursor = conn.execute(
            "SELECT * FROM verification_results WHERE contact_id = ? ORDER BY created_at DESC;",
            (contact_id,),
        )
        return [self._row_to_result(row) for row in cursor.fetchall()]

    def _row_to_result(self, row: sqlite3.Row) -> VerificationResult:
        return VerificationResult(
            id=row["id"],
            contact_id=row["contact_id"],
            task_id=row["task_id"],
            confidence=row["confidence"],
            decision=VerificationDecision(row["decision"]),
            signals_json=row["signals_json"],
            ocr_text=row["ocr_text"],
            created_at=row["created_at"],
        )
