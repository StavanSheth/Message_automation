"""Unit tests for Scheduler and TaskDispatcher task eligibility rules."""

import pytest
from unittest.mock import MagicMock
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.task_repo import TaskRepository
from backend.scheduler.task_dispatcher import TaskDispatcher
from backend.application.throttling_service import ThrottlingService
from backend.domain.models import Task
from backend.domain.enums import TaskState, TaskType, SystemState


@pytest.fixture
def eligibility_env(tmp_path):
    db_path = str(tmp_path / "test_eligibility.db")
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()

    task_repo = TaskRepository(db)
    worker_mgr = MagicMock()
    throttling_mock = MagicMock()
    throttling_mock.can_dispatch.return_value = (True, "OK")
    control_mock = MagicMock()
    control_mock.state = SystemState.RUNNING

    dispatcher = TaskDispatcher(
        task_repo=task_repo,
        worker_manager=worker_mgr,
        throttling_service=throttling_mock,
        control_service=control_mock,
    )
    return dispatcher, task_repo, throttling_mock, control_mock


def test_ready_task_is_eligible(eligibility_env):
    dispatcher, _, _, _ = eligibility_env
    task = Task(id="t-ready", contact_id="c1", type=TaskType.MESSAGE, status=TaskState.READY)
    eligible, reason = dispatcher.is_task_eligible(task)
    assert eligible is True


def test_reconciling_task_is_never_eligible(eligibility_env):
    dispatcher, _, _, _ = eligibility_env
    task = Task(id="t-rec", contact_id="c1", type=TaskType.MESSAGE, status=TaskState.RECONCILING)
    eligible, reason = dispatcher.is_task_eligible(task)
    assert eligible is False
    assert "ineligible" in reason.lower()


def test_manual_review_task_is_never_eligible(eligibility_env):
    dispatcher, _, _, _ = eligibility_env
    task = Task(id="t-rev", contact_id="c1", type=TaskType.MESSAGE, status=TaskState.MANUAL_REVIEW)
    eligible, reason = dispatcher.is_task_eligible(task)
    assert eligible is False
    assert "ineligible" in reason.lower()


def test_cancelled_task_is_never_eligible(eligibility_env):
    dispatcher, _, _, _ = eligibility_env
    task = Task(id="t-can", contact_id="c1", type=TaskType.MESSAGE, status=TaskState.CANCELLED)
    eligible, reason = dispatcher.is_task_eligible(task)
    assert eligible is False


def test_system_paused_blocks_eligibility(eligibility_env):
    dispatcher, _, _, control_mock = eligibility_env
    control_mock.state = SystemState.PAUSED
    task = Task(id="t-paused-check", contact_id="c1", type=TaskType.MESSAGE, status=TaskState.READY)
    eligible, reason = dispatcher.is_task_eligible(task)
    assert eligible is False
    assert "system state" in reason.lower()
