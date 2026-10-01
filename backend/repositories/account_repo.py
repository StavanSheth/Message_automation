"""Repository for Instagram account records and session assignments."""

import sqlite3
from typing import Optional, List
from backend.repositories.base import BaseRepository
from backend.domain.models import Account, utc_now_iso
from backend.domain.enums import AccountStatus


class AccountRepository(BaseRepository):
    """Data access repository for accounts table."""

    def create(self, account: Account) -> Account:
        """Insert a new account."""
        now = utc_now_iso()
        account.created_at = account.created_at or now
        account.updated_at = account.updated_at or now

        query = """
            INSERT INTO accounts (
                id, username, status, profile_path,
                assigned_worker_id, assigned_session_id,
                daily_send_limit, daily_sends_count,
                last_send_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        # Note: 10 parameters
        query = """
            INSERT INTO accounts (
                id, username, status, profile_path,
                assigned_worker_id, assigned_session_id,
                daily_send_limit, daily_sends_count,
                last_send_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        params = (
            account.id,
            account.username,
            account.status.value if isinstance(account.status, AccountStatus) else str(account.status),
            account.profile_path,
            account.assigned_worker_id,
            account.assigned_session_id,
            account.daily_send_limit,
            account.daily_sends_count,
            account.last_send_at,
            account.created_at,
            account.updated_at,
        )
        with self.db.transaction() as conn:
            conn.execute(query, params)
        return account

    def get_by_id(self, account_id: str) -> Optional[Account]:
        """Fetch account by ID."""
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT * FROM accounts WHERE id = ?;", (account_id,))
        row = cursor.fetchone()
        return self._row_to_account(row) if row else None

    def get_by_username(self, username: str) -> Optional[Account]:
        """Fetch account by username."""
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT * FROM accounts WHERE username = ?;", (username,))
        row = cursor.fetchone()
        return self._row_to_account(row) if row else None

    def list_all(self) -> List[Account]:
        """List all accounts."""
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT * FROM accounts ORDER BY created_at ASC;")
        return [self._row_to_account(row) for row in cursor.fetchall()]

    def list_active(self) -> List[Account]:
        """List accounts in ACTIVE status."""
        conn = self.db.get_connection()
        cursor = conn.execute("SELECT * FROM accounts WHERE status = 'ACTIVE' ORDER BY created_at ASC;")
        return [self._row_to_account(row) for row in cursor.fetchall()]

    def update_status(self, account_id: str, status: AccountStatus) -> bool:
        """Update account status."""
        now = utc_now_iso()
        status_val = status.value if isinstance(status, AccountStatus) else str(status)
        query = "UPDATE accounts SET status = ?, updated_at = ? WHERE id = ?;"
        with self.db.transaction() as conn:
            cursor = conn.execute(query, (status_val, now, account_id))
            return cursor.rowcount > 0

    def assign_worker(
        self,
        account_id: str,
        worker_id: Optional[str],
        session_id: Optional[str] = None,
    ) -> bool:
        """Assign or unassign worker and browser session to an account."""
        now = utc_now_iso()
        query = """
            UPDATE accounts SET
                assigned_worker_id = ?,
                assigned_session_id = ?,
                updated_at = ?
            WHERE id = ?;
        """
        with self.db.transaction() as conn:
            cursor = conn.execute(query, (worker_id, session_id, now, account_id))
            return cursor.rowcount > 0

    def increment_daily_sends(self, account_id: str) -> bool:
        """Increment daily send count and update last_send_at."""
        now = utc_now_iso()
        query = """
            UPDATE accounts SET
                daily_sends_count = daily_sends_count + 1,
                last_send_at = ?,
                updated_at = ?
            WHERE id = ?;
        """
        with self.db.transaction() as conn:
            cursor = conn.execute(query, (now, now, account_id))
            return cursor.rowcount > 0

    def reset_daily_sends(self, account_id: str) -> bool:
        """Reset daily send counter for account."""
        now = utc_now_iso()
        query = "UPDATE accounts SET daily_sends_count = 0, updated_at = ? WHERE id = ?;"
        with self.db.transaction() as conn:
            cursor = conn.execute(query, (now, account_id))
            return cursor.rowcount > 0

    def _row_to_account(self, row: sqlite3.Row) -> Account:
        return Account(
            id=row["id"],
            username=row["username"],
            status=AccountStatus(row["status"]) if row["status"] in AccountStatus.__members__ or row["status"] in [s.value for s in AccountStatus] else AccountStatus.ACTIVE,
            profile_path=row["profile_path"],
            assigned_worker_id=row["assigned_worker_id"],
            assigned_session_id=row["assigned_session_id"],
            daily_send_limit=row["daily_send_limit"],
            daily_sends_count=row["daily_sends_count"],
            last_send_at=row["last_send_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
