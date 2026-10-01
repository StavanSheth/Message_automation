"""Integration tests for Phase 4 operational controls, status service, metrics, and worker quarantine."""

import pytest
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.task_repo import TaskRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.worker_repo import WorkerRepository
from backend.repositories.cooldown_repo import CooldownRepository
from backend.repositories.account_repo import AccountRepository
from backend.repositories.manual_review_repo import ManualReviewRepository
from backend.application.lifecycle import ApplicationLifecycleManager
from backend.application.control_service import ApplicationControlService
from backend.application.status_service import StatusService
from backend.application.metrics_service import MetricsService
from backend.application.retention_service import RetentionService
from backend.workers.default_manager import DefaultWorkerManager
from backend.domain.models import Task, Account, WorkerRecord
from backend.domain.enums import TaskState, TaskType, SystemState, WorkerStatus, WorkerMode, AccountStatus


@pytest.fixture
def ops_env(tmp_path):
    db_path = str(tmp_path / "test_ops.db")
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()

    task_repo = TaskRepository(db)
    event_repo = EventRepository(db)
    worker_repo = WorkerRepository(db)
    cooldown_repo = CooldownRepository(db)
    account_repo = AccountRepository(db)
    manual_review_repo = ManualReviewRepository(db)

    worker_mgr = DefaultWorkerManager(
        task_repo=task_repo,
        event_repo=event_repo,
        worker_repo=worker_repo,
    )

    lifecycle = ApplicationLifecycleManager(
        db=db,
        task_repo=task_repo,
        event_repo=event_repo,
        worker_manager=worker_mgr,
        manual_review_repo=manual_review_repo,
    )

    control = ApplicationControlService(
        lifecycle_manager=lifecycle,
        worker_manager=worker_mgr,
        event_repo=event_repo,
    )

    status_svc = StatusService(
        db=db,
        control_service=control,
        worker_manager=worker_mgr,
        account_repo=account_repo,
        cooldown_repo=cooldown_repo,
    )

    metrics_svc = MetricsService(db=db)
    retention_svc = RetentionService(db=db)

    return {
        "db": db,
        "control": control,
        "lifecycle": lifecycle,
        "worker_mgr": worker_mgr,
        "status_svc": status_svc,
        "metrics_svc": metrics_svc,
        "retention_svc": retention_svc,
        "task_repo": task_repo,
        "account_repo": account_repo,
        "worker_repo": worker_repo,
    }


def test_control_service_lifecycle_and_status(ops_env):
    control = ops_env["control"]
    status_svc = ops_env["status_svc"]
    metrics_svc = ops_env["metrics_svc"]

    # Initial status
    stat = status_svc.get_system_status()
    assert stat["system_state"] == SystemState.STOPPED.value

    # Start
    res = control.start()
    assert res["status"] == "ok"
    assert control.state == SystemState.RUNNING

    stat_running = status_svc.get_system_status()
    assert stat_running["system_state"] == SystemState.RUNNING.value

    # Pause
    assert control.pause("routine test") is True
    assert control.state == SystemState.PAUSED

    # Resume
    assert control.resume("end test") is True
    assert control.state == SystemState.RUNNING

    # Metrics
    metrics = metrics_svc.get_metrics()
    assert "tasks_created" in metrics
    assert "success_rate_percent" in metrics


def test_worker_quarantine_operational_control(ops_env):
    worker_mgr = ops_env["worker_mgr"]
    task_repo = ops_env["task_repo"]

    # Start a worker
    rec = worker_mgr.start_worker(mode=WorkerMode.SINGLE_BROWSER)
    worker_id = rec.id

    assert worker_mgr.get_worker_status(worker_id) == WorkerStatus.IDLE

    # Quarantine worker
    quarantined = worker_mgr.quarantine_worker(worker_id, reason="Repeated challenge block")
    assert quarantined is True
    assert worker_mgr.get_worker_status(worker_id) == WorkerStatus.QUARANTINED

    # Quarantined worker does not process tasks
    from backend.repositories.contact_repo import ContactRepository
    from backend.domain.models import Contact
    contact_repo = ContactRepository(ops_env["db"])
    contact_repo.create(Contact(id="c1", name="Test User", instagram_url="https://instagram.com/u1"))

    task = Task(id="t-quar-check", contact_id="c1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)

    processed = worker_mgr.process_tasks()
    assert processed == 0

    # Resume worker from quarantine/degraded
    resumed = worker_mgr.resume_worker(worker_id, reason="Account unblocked")
    assert resumed is True
    assert worker_mgr.get_worker_status(worker_id) == WorkerStatus.IDLE
