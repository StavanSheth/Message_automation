"""Phase 3 unit tests for Task Leases and Lock Expiration (Workstream H)."""

import pytest
import time
from datetime import datetime, timezone, timedelta
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.task_repo import TaskRepository
from backend.domain.models import Task, Contact, utc_now_iso
from backend.domain.enums import TaskState, TaskType, RepliedStatus


@pytest.fixture
def task_repo(tmp_path):
    db = DatabaseManager(str(tmp_path / "test_leases.db"))
    MigrationRunner(db).apply_pending()
    contact_repo = ContactRepository(db)
    contact_repo.create(Contact(id="C-1", name="Test User", instagram_url="https://instagram.com/test_user", replied_status=RepliedStatus.UNKNOWN))
    return TaskRepository(db)


def test_acquire_lease_success(task_repo):
    task = Task(id="T-LEASE-1", contact_id="C-1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)

    lease_id = task_repo.acquire_lease("T-LEASE-1", "worker-1", lease_duration_seconds=60)
    assert lease_id is not None

    t = task_repo.get_by_id("T-LEASE-1")
    assert t.status == TaskState.RUNNING
    assert t.worker_id == "worker-1"
    assert t.lease_id == lease_id
    assert t.lease_owner == "worker-1"
    assert t.lease_expires_at is not None
    assert task_repo.is_lease_valid("T-LEASE-1", lease_id) is True


def test_active_lease_cannot_be_stolen(task_repo):
    task = Task(id="T-LEASE-2", contact_id="C-1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)

    lease1 = task_repo.acquire_lease("T-LEASE-2", "worker-1", lease_duration_seconds=300)
    assert lease1 is not None

    # Second worker tries to acquire active lease -> MUST fail
    lease2 = task_repo.acquire_lease("T-LEASE-2", "worker-2", lease_duration_seconds=300)
    assert lease2 is None

    t = task_repo.get_by_id("T-LEASE-2")
    assert t.lease_owner == "worker-1"
    assert t.lease_id == lease1


def test_expired_lease_can_be_recovered_by_another_worker(task_repo):
    task = Task(id="T-LEASE-3", contact_id="C-1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)

    # Acquire lease with negative duration so it expires immediately while status is RUNNING
    lease1 = task_repo.acquire_lease("T-LEASE-3", "worker-1", lease_duration_seconds=-10)
    assert lease1 is not None
    assert task_repo.is_lease_valid("T-LEASE-3", lease1) is False

    # Direct stealing of an expired RUNNING task MUST fail (preventing double-execution)
    lease2 = task_repo.acquire_lease("T-LEASE-3", "worker-2", lease_duration_seconds=120)
    assert lease2 is None

    # Safe recovery transitions expired RUNNING task -> INTERRUPTED
    recovered = task_repo.recover_expired_lease("T-LEASE-3")
    assert recovered is not None
    assert recovered.status == TaskState.INTERRUPTED
    assert recovered.lease_id is None

    # Once recovery policy resets task to READY, worker-2 can acquire it
    task_repo.update_state("T-LEASE-3", TaskState.READY, enforce_transition=False)
    lease3 = task_repo.acquire_lease("T-LEASE-3", "worker-2", lease_duration_seconds=120)
    assert lease3 is not None
    assert lease3 != lease1

    t = task_repo.get_by_id("T-LEASE-3")
    assert t.lease_owner == "worker-2"
    assert t.lease_id == lease3


def test_lease_renewal(task_repo):
    task = Task(id="T-LEASE-4", contact_id="C-1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)

    lease_id = task_repo.acquire_lease("T-LEASE-4", "worker-1", lease_duration_seconds=10)
    assert lease_id is not None

    # Owner renews lease
    renewed = task_repo.renew_lease("T-LEASE-4", lease_id, "worker-1", lease_duration_seconds=600)
    assert renewed is True

    # Non-owner cannot renew lease
    failed_renew = task_repo.renew_lease("T-LEASE-4", lease_id, "worker-imposter", lease_duration_seconds=600)
    assert failed_renew is False


def test_only_lease_owner_can_release(task_repo):
    task = Task(id="T-LEASE-5", contact_id="C-1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)

    lease_id = task_repo.acquire_lease("T-LEASE-5", "worker-1", lease_duration_seconds=120)
    assert lease_id is not None

    # Imposter cannot release
    assert task_repo.release_lease("T-LEASE-5", lease_id, "worker-imposter") is False

    # True owner can release
    assert task_repo.release_lease("T-LEASE-5", lease_id, "worker-1") is True
    t = task_repo.get_by_id("T-LEASE-5")
    assert t.lease_id is None
    assert t.lease_owner is None
