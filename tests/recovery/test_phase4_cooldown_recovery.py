"""Recovery tests for persistent rate-limit cooldowns and durable send delays across restarts."""

import pytest
from datetime import datetime, timezone, timedelta
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.cooldown_repo import CooldownRepository
from backend.repositories.account_repo import AccountRepository
from backend.application.throttling_service import ThrottlingService
from backend.domain.models import Account, utc_now_iso


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test_cd_recovery.db")
    db = DatabaseManager(path)
    MigrationRunner(db).apply_pending()
    return path


def test_rate_limit_cooldown_persists_across_restart(db_path):
    db1 = DatabaseManager(db_path)
    cd_repo1 = CooldownRepository(db1)
    acc_repo1 = AccountRepository(db1)
    acc_repo1.create(Account(id="acc-cd-1", username="user_cd1", status="ACTIVE"))

    throttling1 = ThrottlingService(cooldown_repo=cd_repo1, account_repo=acc_repo1)
    throttling1.trigger_rate_limit(reason="Rate limited by IG", account_id="acc-cd-1", duration_seconds=1800)

    # Process restart
    db2 = DatabaseManager(db_path)
    cd_repo2 = CooldownRepository(db2)
    acc_repo2 = AccountRepository(db2)
    throttling2 = ThrottlingService(cooldown_repo=cd_repo2, account_repo=acc_repo2)

    # Account must remain blocked by active cooldown
    allowed, reason = throttling2.can_dispatch(account_id="acc-cd-1")
    assert allowed is False
    assert "cooldown active until" in reason.lower()


def test_minimum_send_delay_persists_across_restart(db_path):
    db1 = DatabaseManager(db_path)
    cd_repo1 = CooldownRepository(db1)
    acc_repo1 = AccountRepository(db1)
    acc_repo1.create(Account(id="acc-delay-1", username="user_delay1", status="ACTIVE"))

    throttling1 = ThrottlingService(cooldown_repo=cd_repo1, account_repo=acc_repo1)
    # Record a send right now
    throttling1.record_send(account_id="acc-delay-1")

    # Immediate restart: delay is 5s by default, 0s has elapsed
    db2 = DatabaseManager(db_path)
    cd_repo2 = CooldownRepository(db2)
    acc_repo2 = AccountRepository(db2)
    throttling2 = ThrottlingService(cooldown_repo=cd_repo2, account_repo=acc_repo2)

    # Must be BLOCKED due to durable minimum send delay surviving restart
    allowed, reason = throttling2.can_dispatch(account_id="acc-delay-1")
    assert allowed is False
    assert "minimum delay throttling" in reason.lower()
