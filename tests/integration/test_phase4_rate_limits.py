"""Integration tests for Scheduler and ThrottlingService integration."""

import pytest
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.task_repo import TaskRepository
from backend.repositories.followup_repo import FollowupRepository
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.cooldown_repo import CooldownRepository
from backend.repositories.account_repo import AccountRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.worker_repo import WorkerRepository
from backend.application.throttling_service import ThrottlingService
from backend.scheduler.scheduler import Scheduler
from backend.scheduler.task_dispatcher import TaskDispatcher
from backend.workers.default_manager import DefaultWorkerManager
from backend.domain.models import Task, Account, Contact
from backend.domain.enums import TaskState, TaskType, AccountStatus, WorkerMode


@pytest.fixture
def sched_throttle_env(tmp_path):
    db_path = str(tmp_path / "test_sched_throttle.db")
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()

    task_repo = TaskRepository(db)
    fu_repo = FollowupRepository(db)
    contact_repo = ContactRepository(db)
    cooldown_repo = CooldownRepository(db)
    account_repo = AccountRepository(db)
    event_repo = EventRepository(db)
    worker_repo = WorkerRepository(db)

    contact_repo.create(Contact(id="c-throttle-1", name="Throttle Contact", instagram_url="https://instagram.com/throt1"))

    throttling_svc = ThrottlingService(
        cooldown_repo=cooldown_repo,
        account_repo=account_repo,
        event_repo=event_repo,
    )

    worker_mgr = DefaultWorkerManager(
        task_repo=task_repo,
        event_repo=event_repo,
        worker_repo=worker_repo,
    )

    scheduler = Scheduler(
        task_repo=task_repo,
        followup_repo=fu_repo,
        contact_repo=contact_repo,
        worker_manager=worker_mgr,
        throttling_service=throttling_svc,
    )

    return {
        "db": db,
        "scheduler": scheduler,
        "throttling_svc": throttling_svc,
        "task_repo": task_repo,
        "account_repo": account_repo,
        "worker_mgr": worker_mgr,
    }


def test_cooldown_blocks_scheduler_dispatch(sched_throttle_env):
    scheduler = sched_throttle_env["scheduler"]
    throttling_svc = sched_throttle_env["throttling_svc"]
    task_repo = sched_throttle_env["task_repo"]

    task = Task(id="t-throt-1", contact_id="c-throttle-1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)

    # Trigger global cooldown
    throttling_svc.trigger_rate_limit(reason="Rate limited by platform", duration_seconds=120)

    # Tick should not dispatch
    ready = scheduler.tick()
    assert len(ready) == 1

    t = task_repo.get_by_id("t-throt-1")
    assert t.status == TaskState.READY  # Remains safe in READY state, not claimed
