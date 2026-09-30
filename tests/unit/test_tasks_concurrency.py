"""Concurrency tests for task claiming to ensure two workers cannot claim the same task."""

import concurrent.futures
import pytest
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.task_repo import TaskRepository
from backend.domain.models import Contact, Task
from backend.domain.enums import TaskType, TaskState


def test_concurrent_worker_claiming_exact_single_winner(tmp_path):
    db_file = str(tmp_path / "concurrent_tasks.db")
    db = DatabaseManager(db_file)
    MigrationRunner(db).apply_pending()

    contact_repo = ContactRepository(db)
    task_repo = TaskRepository(db)

    # Seed contact & ready task
    contact = Contact(
        id="C-CONCURRENT",
        name="Concurrent User",
        instagram_url="https://instagram.com/concurrent",
    )
    contact_repo.create(contact)

    task = Task(
        id="T-CONCURRENT-1",
        contact_id="C-CONCURRENT",
        type=TaskType.MESSAGE,
        status=TaskState.READY,
    )
    task_repo.create(task)

    num_workers = 10
    results = []

    def try_claim(worker_idx: int) -> bool:
        # Create thread-specific database connection manager to simulate separate workers
        thread_db = DatabaseManager(db_file)
        thread_task_repo = TaskRepository(thread_db)
        worker_id = f"WORKER-{worker_idx:02d}"
        lock_token = f"LOCK-{worker_idx:02d}"
        claimed = thread_task_repo.claim_task("T-CONCURRENT-1", worker_id=worker_id, lock_token=lock_token)
        thread_db.close()
        return claimed

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = [executor.submit(try_claim, i) for i in range(num_workers)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    # Exactly ONE worker must have successfully claimed the task
    assert results.count(True) == 1
    assert results.count(False) == num_workers - 1

    # Verify task in DB is RUNNING and claimed
    final_task = task_repo.get_by_id("T-CONCURRENT-1")
    assert final_task.status == TaskState.RUNNING
    assert final_task.worker_id is not None
    assert final_task.lock_token is not None
