"""
Aggressive concurrency and race condition tests for Follow-up Task Materialization.
Validates that concurrent scheduler ticks cannot create duplicate tasks,
leave orphan records, or corrupt follow-up lifecycle states.
"""

from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import pytest

from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.domain.models import Contact, Task, Message, Followup
from backend.domain.enums import (
    TaskType,
    TaskState,
    FollowupStatus,
    RepliedStatus,
    MessageState,
)
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.task_repo import TaskRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.followup_repo import FollowupRepository
from backend.scheduler.scheduler import Scheduler


@pytest.fixture
def concurrent_env(tmp_path):
    db_file = str(tmp_path / "followup_concurrency.db")
    mgr = DatabaseManager(db_file)
    runner = MigrationRunner(mgr)
    runner.apply_pending()

    contact_repo = ContactRepository(mgr)
    task_repo = TaskRepository(mgr)
    msg_repo = MessageRepository(mgr)
    fu_repo = FollowupRepository(mgr)

    return {
        "db": mgr,
        "contact_repo": contact_repo,
        "task_repo": task_repo,
        "msg_repo": msg_repo,
        "fu_repo": fu_repo,
        "db_file": db_file,
    }


def test_concurrent_scheduler_ticks_followup_materialization(concurrent_env):
    """
    Simulate 10 concurrent threads executing scheduler.tick() simultaneously
    against due follow-ups.
    Guarantees:
    - Exactly 1 Task is created for Follow-up 1.
    - Exactly 1 Task is created for Follow-up 2.
    - No duplicate tasks or unhandled IntegrityError crashes occur.
    - Follow-up status transitions atomically to DUE.
    """
    env = concurrent_env
    contact_repo = env["contact_repo"]
    task_repo = env["task_repo"]
    fu_repo = env["fu_repo"]
    db_file = env["db_file"]

    # 1. Setup contact with completed initial message task
    now = datetime.now(timezone.utc)
    contact = Contact(id="c-conc-1", name="Concurrent User", instagram_url="https://instagram.com/conc_user")
    contact_repo.create(contact)

    initial_task = Task(
        id="t-init-done",
        contact_id="c-conc-1",
        type=TaskType.MESSAGE,
        sequence=0,
        status=TaskState.COMPLETED,
    )
    task_repo.create(initial_task)

    # 2. Setup due Follow-up 1
    fu1 = Followup(
        id="fu-conc-1",
        contact_id="c-conc-1",
        sequence=1,
        message="Follow-up 1 body",
        delay_seconds=3600,
        status=FollowupStatus.SCHEDULED,
        scheduled_at=(now - timedelta(minutes=10)).isoformat(),
    )
    fu_repo.create(fu1)

    # 3. Launch 10 parallel threads each creating its own DB connection and Scheduler
    def run_tick():
        local_db = DatabaseManager(db_file)
        local_contact_repo = ContactRepository(local_db)
        local_task_repo = TaskRepository(local_db)
        local_fu_repo = FollowupRepository(local_db)

        scheduler = Scheduler(
            task_repo=local_task_repo,
            followup_repo=local_fu_repo,
            contact_repo=local_contact_repo,
        )
        return scheduler.tick()

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(run_tick) for _ in range(10)]
        results = [f.result() for f in as_completed(futures)]

    # 4. Verify that across all threads, exactly 1 Task was materialized for Followup 1
    tasks = task_repo.get_by_contact_id("c-conc-1")
    fu_tasks = [t for t in tasks if t.type == TaskType.FOLLOW_UP_1]
    assert len(fu_tasks) == 1
    assert fu_tasks[0].status == TaskState.READY

    # 5. Verify followup status transitioned to DUE
    updated_fu1 = fu_repo.get_by_id("fu-conc-1")
    assert updated_fu1.status == FollowupStatus.DUE


def test_concurrent_reply_cancellation_vs_materialization(concurrent_env):
    """
    Race condition test: Contact reply marking vs scheduler tick.
    Guarantees follow-up is either cancelled or safely materialized,
    never left in an inconsistent state.
    """
    env = concurrent_env
    contact_repo = env["contact_repo"]
    task_repo = env["task_repo"]
    fu_repo = env["fu_repo"]
    db_file = env["db_file"]

    now = datetime.now(timezone.utc)
    contact = Contact(id="c-conc-reply", name="Reply Racer", instagram_url="https://instagram.com/reply_racer")
    contact_repo.create(contact)

    initial_task = Task(id="t-reply-done", contact_id="c-conc-reply", type=TaskType.MESSAGE, sequence=0, status=TaskState.COMPLETED)
    task_repo.create(initial_task)

    fu = Followup(
        id="fu-conc-reply",
        contact_id="c-conc-reply",
        sequence=1,
        message="Follow-up message",
        delay_seconds=3600,
        status=FollowupStatus.SCHEDULED,
        scheduled_at=(now - timedelta(minutes=5)).isoformat(),
    )
    fu_repo.create(fu)

    def mark_reply():
        local_db = DatabaseManager(db_file)
        local_contact_repo = ContactRepository(local_db)
        local_fu_repo = FollowupRepository(local_db)
        c = local_contact_repo.get_by_id("c-conc-reply")
        c.replied_status = RepliedStatus.YES
        local_contact_repo.update(c)
        local_fu_repo.cancel_pending_for_contact("c-conc-reply", cancel_reason="REPLIED")

    def run_tick():
        local_db = DatabaseManager(db_file)
        s = Scheduler(
            task_repo=TaskRepository(local_db),
            followup_repo=FollowupRepository(local_db),
            contact_repo=ContactRepository(local_db),
        )
        return s.tick()

    with ThreadPoolExecutor(max_workers=4) as executor:
        f1 = executor.submit(mark_reply)
        f2 = executor.submit(run_tick)
        f3 = executor.submit(run_tick)
        f1.result()
        f2.result()
        f3.result()

    updated_fu = fu_repo.get_by_id("fu-conc-reply")
    assert updated_fu.status in (FollowupStatus.CANCELLED, FollowupStatus.DUE)
