"""Throttling and rate-limiting service enforcing execution rates, cooldowns, and account limits."""

import time
from typing import Optional, Tuple, Dict
from datetime import datetime, timezone, timedelta
from backend.domain.models import RateLimitCooldown, Account, utc_now_iso
from backend.domain.enums import EventCode, EventLevel, AccountStatus
from backend.repositories.cooldown_repo import CooldownRepository
from backend.repositories.account_repo import AccountRepository
from backend.repositories.event_repo import EventRepository
from backend.config.settings import AppSettings, get_settings
from backend.events.correlation import generate_id
from backend.events.logger import get_logger

logger = get_logger("throttling_service")


class ThrottlingService:
    """
    Centralized execution throttling coordinator enforcing:
    - Account-level daily send quotas and account status
    - Global and account-specific cooldowns
    - Minimum delays between message sends
    - Cooldown triggers on rate limits and errors
    """

    def __init__(
        self,
        cooldown_repo: CooldownRepository,
        account_repo: Optional[AccountRepository] = None,
        event_repo: Optional[EventRepository] = None,
        settings: Optional[AppSettings] = None,
    ):
        self.cooldown_repo = cooldown_repo
        self.account_repo = account_repo
        self.event_repo = event_repo
        self.settings = settings or get_settings()
        self._last_send_timestamps: Dict[str, float] = {}

    def can_dispatch(
        self,
        account_id: Optional[str] = None,
        worker_id: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """
        Check if an execution is permitted under current rate limits and cooldowns:
        1. Check global cooldown
        2. Check account cooldown if account_id is present
        3. Check account limits and status
        4. Check minimum delay between sends
        Returns (allowed, reason).
        """
        # 1. Global cooldown
        global_cd = self.cooldown_repo.get_active_cooldown(scope="GLOBAL")
        if global_cd:
            return False, f"Global cooldown active until {global_cd.cooldown_until} ({global_cd.reason})"

        # 2. Account cooldown
        if account_id:
            account_cd = self.cooldown_repo.get_active_cooldown(scope="ACCOUNT", account_id=account_id)
            if account_cd:
                return False, f"Account {account_id} cooldown active until {account_cd.cooldown_until} ({account_cd.reason})"

            # 3. Account status and quota limits
            if self.account_repo:
                account = self.account_repo.get_by_id(account_id)
                if account:
                    if account.status != AccountStatus.ACTIVE.value:
                        return False, f"Account {account_id} is not active (status: {account.status})"
                    if account.daily_sends_count >= account.daily_send_limit:
                        return False, f"Account {account_id} reached daily send limit ({account.daily_sends_count}/{account.daily_send_limit})"

        # 4. Minimum delay check
        min_delay = getattr(self.settings, "minimum_send_delay_seconds", 5.0)
        now_ts = time.time()
        key = account_id or worker_id or "global"
        last_ts = self._last_send_timestamps.get(key, 0.0)
        if (now_ts - last_ts) < min_delay:
            remaining = round(min_delay - (now_ts - last_ts), 2)
            return False, f"Minimum delay throttling ({remaining}s remaining for {key})"

        return True, "Dispatch permitted"

    def record_send(self, account_id: Optional[str] = None, worker_id: Optional[str] = None) -> None:
        """Update last send timestamp and increment account count."""
        now_ts = time.time()
        if account_id:
            self._last_send_timestamps[account_id] = now_ts
            if self.account_repo:
                self.account_repo.increment_daily_sends(account_id)
        if worker_id:
            self._last_send_timestamps[worker_id] = now_ts
        self._last_send_timestamps["global"] = now_ts

    def trigger_rate_limit(
        self,
        reason: str = "Rate limit detected",
        account_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        session_id: Optional[str] = None,
        duration_seconds: int = 3600,
    ) -> RateLimitCooldown:
        """Trigger rate limit cooldown, record in repo, and emit audit event."""
        now = datetime.now(timezone.utc)
        until = now + timedelta(seconds=duration_seconds)
        scope = "ACCOUNT" if account_id else "GLOBAL"

        cd = RateLimitCooldown(
            id=generate_id("CD"),
            scope=scope,
            account_id=account_id,
            reason=reason,
            error_code="RATE_LIMITED",
            detected_at=now.isoformat(),
            cooldown_until=until.isoformat(),
            detected_by_worker=worker_id,
            detected_by_session=session_id,
            is_active=True,
        )
        saved = self.cooldown_repo.record_cooldown(cd)

        if self.event_repo:
            self.event_repo.record(
                event_code=EventCode.RATE_LIMIT_TRIGGERED,
                category="throttling",
                level=EventLevel.WARNING,
                entity_type="account" if account_id else "system",
                entity_id=account_id,
                payload={
                    "scope": scope,
                    "reason": reason,
                    "cooldown_until": until.isoformat(),
                    "worker_id": worker_id,
                },
            )
            self.event_repo.record(
                event_code=EventCode.COOLDOWN_STARTED,
                category="throttling",
                level=EventLevel.WARNING,
                entity_type="account" if account_id else "system",
                entity_id=account_id,
                payload={"cooldown_id": saved.id, "duration_seconds": duration_seconds},
            )

        logger.warning(f"Rate limit cooldown triggered ({scope}): {reason}, until {until.isoformat()}")
        return saved

    def trigger_error_cooldown(
        self,
        reason: str = "Error cooldown",
        account_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        duration_seconds: int = 300,
    ) -> RateLimitCooldown:
        """Trigger temporary cooldown after repeated errors."""
        now = datetime.now(timezone.utc)
        until = now + timedelta(seconds=duration_seconds)
        scope = "ACCOUNT" if account_id else "GLOBAL"

        cd = RateLimitCooldown(
            id=generate_id("CD"),
            scope=scope,
            account_id=account_id,
            reason=reason,
            error_code="ERROR_COOLDOWN",
            detected_at=now.isoformat(),
            cooldown_until=until.isoformat(),
            detected_by_worker=worker_id,
            is_active=True,
        )
        saved = self.cooldown_repo.record_cooldown(cd)

        if self.event_repo:
            self.event_repo.record(
                event_code=EventCode.COOLDOWN_STARTED,
                category="throttling",
                level=EventLevel.INFO,
                entity_type="account" if account_id else "system",
                entity_id=account_id,
                payload={"cooldown_id": saved.id, "reason": reason, "duration_seconds": duration_seconds},
            )
        return saved
