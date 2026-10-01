"""Unit tests for AccountService and Account / Session / Worker ownership."""

import pytest
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.account_repo import AccountRepository
from backend.repositories.worker_repo import WorkerRepository
from backend.application.account_service import AccountService
from backend.domain.models import Account, WorkerRecord
from backend.domain.enums import AccountStatus, WorkerMode, WorkerStatus


@pytest.fixture
def account_env(tmp_path):
    db_path = str(tmp_path / "test_account.db")
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()

    account_repo = AccountRepository(db)
    worker_repo = WorkerRepository(db)
    svc = AccountService(account_repo=account_repo, worker_repo=worker_repo)
    return svc, account_repo, worker_repo


def test_register_and_get_account(account_env):
    svc, account_repo, _ = account_env
    acc = svc.register_account("agent_user_1", profile_path="/profiles/u1", daily_limit=50)
    assert acc.id is not None
    assert acc.username == "agent_user_1"
    assert acc.status == AccountStatus.ACTIVE.value

    fetched = svc.get_account(acc.id)
    assert fetched is not None
    assert fetched.username == "agent_user_1"


def test_assign_worker_and_session(account_env):
    svc, account_repo, worker_repo = account_env
    acc = svc.register_account("agent_user_2")
    wkr = WorkerRecord(
        id="WKR-ACC-1",
        worker_code="worker-acc-1",
        mode=WorkerMode.SINGLE_BROWSER,
        status=WorkerStatus.IDLE,
    )
    worker_repo.create(wkr)

    assigned = svc.assign_worker_to_account(acc.id, "WKR-ACC-1", "SESS-ACC-1")
    assert assigned is True

    updated_acc = svc.get_account(acc.id)
    assert updated_acc.assigned_worker_id == "WKR-ACC-1"
    assert updated_acc.assigned_session_id == "SESS-ACC-1"

    updated_wkr = worker_repo.get_by_id("WKR-ACC-1")
    assert updated_wkr.account_id == acc.id


def test_can_send_checks(account_env):
    svc, _, _ = account_env
    acc = svc.register_account("agent_user_3", daily_limit=1)
    can, _ = svc.can_send(acc.id)
    assert can is True

    svc.record_send(acc.id)
    can_after, reason = svc.can_send(acc.id)
    assert can_after is False
    assert "limit" in reason.lower()
