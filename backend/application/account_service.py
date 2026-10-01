"""Account and session management service enforcing account ownership and send quotas."""

from typing import Optional, List, Dict, Any, Tuple
from backend.domain.models import Account, utc_now_iso
from backend.domain.enums import AccountStatus, EventCode, EventLevel
from backend.repositories.account_repo import AccountRepository
from backend.repositories.event_repo import EventRepository
from backend.events.correlation import generate_id
from backend.events.logger import get_logger

logger = get_logger("account_service")


class AccountService:
    """
    Manages Instagram account lifecycle and worker/session binding:
    - Account -> BrowserProfile -> BrowserSession -> Worker
    - Enforces that each worker is strictly bound to an account.
    - Tracks and enforces daily send limits.
    - Manages account operational status (ACTIVE, PAUSED, CHALLENGED, SUSPENDED).
    """

    def __init__(
        self,
        account_repo: AccountRepository,
        event_repo: Optional[EventRepository] = None,
        worker_repo: Optional[Any] = None,
    ):
        self.account_repo = account_repo
        self.event_repo = event_repo
        self.worker_repo = worker_repo

    def register_account(
        self,
        username: str,
        profile_path: Optional[str] = None,
        daily_send_limit: int = 50,
        daily_limit: Optional[int] = None,
    ) -> Account:
        """Register a new Instagram account."""
        limit = daily_limit if daily_limit is not None else daily_send_limit
        existing = self.account_repo.get_by_username(username)
        if existing:
            return existing

        account = Account(
            id=generate_id("ACC"),
            username=username,
            status=AccountStatus.ACTIVE,
            profile_path=profile_path,
            daily_send_limit=limit,
            daily_sends_count=0,
        )
        created = self.account_repo.create(account)
        logger.info(f"Registered account {username} (id={created.id})")
        return created

    def get_account(self, account_id: str) -> Optional[Account]:
        """Fetch account record by ID."""
        return self.account_repo.get_by_id(account_id)

    def assign_worker_to_account(
        self,
        account_id: str,
        worker_id: str,
        session_id: Optional[str] = None,
    ) -> bool:
        """Bind a worker and browser session to an account."""
        account = self.account_repo.get_by_id(account_id)
        if not account:
            logger.error(f"Cannot assign worker to non-existent account {account_id}")
            return False

        success = self.account_repo.assign_worker(account_id, worker_id, session_id)
        if success and self.worker_repo:
            try:
                wkr = self.worker_repo.get_by_id(worker_id)
                if wkr:
                    wkr.account_id = account_id
                    self.worker_repo.update(wkr)
            except Exception as e:
                logger.warning(f"Failed to update worker {worker_id} account_id: {e}")
        if success and self.event_repo:
            self.event_repo.record(
                event_code=EventCode.WORKER_STARTED,
                category="account",
                level=EventLevel.INFO,
                entity_type="account",
                entity_id=account_id,
                worker_id=worker_id,
                session_id=session_id,
                payload={"account_id": account_id, "worker_id": worker_id, "session_id": session_id},
            )
        return success

    def release_worker_from_account(self, account_id: str) -> bool:
        """Unbind any worker assigned to the account."""
        return self.account_repo.assign_worker(account_id, None, None)

    def can_send(self, account_id: str) -> Tuple[bool, str]:
        """Check if account is eligible to send (ACTIVE status and within daily limit)."""
        account = self.account_repo.get_by_id(account_id)
        if not account:
            return False, "Account not found"
        st = account.status.value if hasattr(account.status, "value") else str(account.status)
        if st != AccountStatus.ACTIVE.value:
            logger.warning(f"Account {account_id} cannot send: status is {st}")
            return False, f"Account status is {st}"
        if account.daily_sends_count >= account.daily_send_limit:
            logger.warning(f"Account {account_id} reached daily send limit ({account.daily_sends_count}/{account.daily_send_limit})")
            return False, f"Account reached daily send limit ({account.daily_sends_count}/{account.daily_send_limit})"
        return True, "Eligible"

    def record_send(self, account_id: str) -> bool:
        """Record an executed send for the account."""
        return self.account_repo.increment_daily_sends(account_id)

    def update_status(self, account_id: str, status: AccountStatus, reason: Optional[str] = None) -> bool:
        """Update account operational status and log audit event."""
        account = self.account_repo.get_by_id(account_id)
        if not account:
            return False

        updated = self.account_repo.update_status(account_id, status)
        if updated and self.event_repo:
            level = EventLevel.WARNING if status in (AccountStatus.CHALLENGED, AccountStatus.SUSPENDED) else EventLevel.INFO
            self.event_repo.record(
                event_code=EventCode.CONTACT_UPDATED,
                category="account",
                level=level,
                entity_type="account",
                entity_id=account_id,
                payload={"status": status.value, "reason": reason},
            )
        return updated
