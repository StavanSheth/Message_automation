"""Repository for durable rate limit and access block cooldowns."""

import sqlite3
from datetime import datetime, timezone
from typing import Optional, List
from backend.repositories.base import BaseRepository
from backend.domain.models import RateLimitCooldown, utc_now_iso


class CooldownRepository(BaseRepository):
    """Data access repository for persistent rate limit and access block cooldowns."""

    def record_cooldown(self, cooldown: RateLimitCooldown) -> RateLimitCooldown:
        """Record a persistent cooldown period."""
        query = """
            INSERT INTO rate_limit_cooldowns (
                id, scope, account_id, reason, error_code,
                detected_at, cooldown_until, detected_by_worker,
                detected_by_session, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        params = (
            cooldown.id,
            cooldown.scope,
            cooldown.account_id,
            cooldown.reason,
            cooldown.error_code,
            cooldown.detected_at,
            cooldown.cooldown_until,
            cooldown.detected_by_worker,
            cooldown.detected_by_session,
            1 if cooldown.is_active else 0,
        )
        with self.db.transaction() as conn:
            conn.execute(query, params)
        return cooldown

    def get_active_cooldown(
        self, scope: str = "GLOBAL", account_id: Optional[str] = None
    ) -> Optional[RateLimitCooldown]:
        """Fetch unexpired active cooldown for scope/account."""
        now_iso = utc_now_iso()
        conn = self.db.get_connection()
        if account_id:
            query = """
                SELECT * FROM rate_limit_cooldowns
                WHERE (scope = 'GLOBAL' OR (scope = 'ACCOUNT' AND account_id = ?))
                  AND is_active = 1
                  AND cooldown_until > ?
                ORDER BY cooldown_until DESC LIMIT 1;
            """
            cursor = conn.execute(query, (account_id, now_iso))
        else:
            query = """
                SELECT * FROM rate_limit_cooldowns
                WHERE is_active = 1
                  AND cooldown_until > ?
                ORDER BY cooldown_until DESC LIMIT 1;
            """
            cursor = conn.execute(query, (now_iso,))

        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_cooldown(row)

    def list_active_cooldowns(self) -> List[RateLimitCooldown]:
        """List all active cooldowns that haven't expired."""
        now_iso = utc_now_iso()
        conn = self.db.get_connection()
        query = """
            SELECT * FROM rate_limit_cooldowns
            WHERE is_active = 1 AND cooldown_until > ?
            ORDER BY cooldown_until DESC;
        """
        cursor = conn.execute(query, (now_iso,))
        return [self._row_to_cooldown(row) for row in cursor.fetchall()]

    def deactivate_cooldown(self, cooldown_id: str) -> bool:
        """Deactivate a cooldown."""
        query = "UPDATE rate_limit_cooldowns SET is_active = 0 WHERE id = ?;"
        with self.db.transaction() as conn:
            cursor = conn.execute(query, (cooldown_id,))
            return cursor.rowcount > 0

    def _row_to_cooldown(self, row: sqlite3.Row) -> RateLimitCooldown:
        return RateLimitCooldown(
            id=row["id"],
            scope=row["scope"],
            account_id=row["account_id"],
            reason=row["reason"],
            error_code=row["error_code"],
            detected_at=row["detected_at"],
            cooldown_until=row["cooldown_until"],
            detected_by_worker=row["detected_by_worker"],
            detected_by_session=row["detected_by_session"],
            is_active=bool(row["is_active"]),
        )
