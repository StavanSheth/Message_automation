"""Unit tests for ThrottlingService, cooldowns, and rate limiting."""

import time
import pytest
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.cooldown_repo import CooldownRepository
from backend.repositories.account_repo import AccountRepository
from backend.repositories.event_repo import EventRepository
from backend.application.throttling_service import ThrottlingService
from backend.domain.models import Account
from backend.domain.enums import AccountStatus


@pytest.fixture
def throttle_env(tmp_path):
    db_path = str(tmp_path / "test_throttle.db")
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()

    cooldown_repo = CooldownRepository(db)
    account_repo = AccountRepository(db)
    event_repo = EventRepository(db)

    svc = ThrottlingService(
        cooldown_repo=cooldown_repo,
        account_repo=account_repo,
        event_repo=event_repo,
    )
    return svc, cooldown_repo, account_repo, event_repo


def test_initial_dispatch_permitted(throttle_env):
    svc, _, _, _ = throttle_env
    allowed, reason = svc.can_dispatch()
    assert allowed is True


def test_global_cooldown_blocks_dispatch(throttle_env):
    svc, cooldown_repo, _, _ = throttle_env
    svc.trigger_rate_limit(reason="High 429 response rate", duration_seconds=60)

    allowed, reason = svc.can_dispatch()
    assert allowed is False
    assert "Global cooldown active" in reason


def test_account_cooldown_blocks_only_that_account(throttle_env):
    svc, cooldown_repo, account_repo, _ = throttle_env
    account_repo.create(Account(id="acc-1", username="user1", status=AccountStatus.ACTIVE.value))
    account_repo.create(Account(id="acc-2", username="user2", status=AccountStatus.ACTIVE.value))

    svc.trigger_rate_limit(account_id="acc-1", reason="Challenge received", duration_seconds=60)

    # acc-1 blocked
    allowed1, reason1 = svc.can_dispatch(account_id="acc-1")
    assert allowed1 is False
    assert "acc-1 cooldown active" in reason1

    # acc-2 permitted
    allowed2, reason2 = svc.can_dispatch(account_id="acc-2")
    assert allowed2 is True


def test_daily_send_limit_enforced(throttle_env):
    svc, _, account_repo, _ = throttle_env
    acc = account_repo.create(
        Account(
            id="acc-limited",
            username="limited_user",
            status=AccountStatus.ACTIVE.value,
            daily_send_limit=2,
            daily_sends_count=0,
        )
    )

    allowed, _ = svc.can_dispatch(account_id="acc-limited")
    assert allowed is True

    svc.record_send(account_id="acc-limited")
    svc.record_send(account_id="acc-limited")

    # Now at limit
    allowed, reason = svc.can_dispatch(account_id="acc-limited")
    assert allowed is False
    assert "daily send limit" in reason.lower()
