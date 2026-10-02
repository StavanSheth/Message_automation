"""Recovery tests for persistent system controls and state survival across restarts."""

import pytest
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.system_control_repo import SystemControlRepository
from backend.application.lifecycle import ApplicationLifecycleManager
from backend.application.control_service import ApplicationControlService
from backend.repositories.task_repo import TaskRepository
from backend.domain.enums import SystemState


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test_ctrl_recovery.db")
    db = DatabaseManager(path)
    MigrationRunner(db).apply_pending()
    return path


def test_paused_system_state_persists_across_restart(db_path):
    db1 = DatabaseManager(db_path)
    task_repo1 = TaskRepository(db1)
    lifecycle1 = ApplicationLifecycleManager(db=db1, task_repo=task_repo1)
    ctrl1 = ApplicationControlService(lifecycle_manager=lifecycle1)

    # Transition to STARTING -> RUNNING -> PAUSED
    ctrl1._transition_to(SystemState.STARTING)
    ctrl1._transition_to(SystemState.RUNNING)
    ctrl1.pause("Maintenance window")
    assert ctrl1.state == SystemState.PAUSED

    # Simulate process crash / new process restart
    db2 = DatabaseManager(db_path)
    task_repo2 = TaskRepository(db2)
    lifecycle2 = ApplicationLifecycleManager(db=db2, task_repo=task_repo2)
    ctrl2 = ApplicationControlService(lifecycle_manager=lifecycle2)

    # Authoritative state must be recovered as PAUSED, not reset to STOPPED
    assert ctrl2.state == SystemState.PAUSED


def test_draining_system_state_persists_across_restart(db_path):
    db1 = DatabaseManager(db_path)
    task_repo1 = TaskRepository(db1)
    lifecycle1 = ApplicationLifecycleManager(db=db1, task_repo=task_repo1)
    ctrl1 = ApplicationControlService(lifecycle_manager=lifecycle1)

    ctrl1._transition_to(SystemState.STARTING)
    ctrl1._transition_to(SystemState.RUNNING)
    ctrl1._transition_to(SystemState.DRAINING)
    assert ctrl1.state == SystemState.DRAINING

    # Restart
    db2 = DatabaseManager(db_path)
    task_repo2 = TaskRepository(db2)
    lifecycle2 = ApplicationLifecycleManager(db=db2, task_repo=task_repo2)
    ctrl2 = ApplicationControlService(lifecycle_manager=lifecycle2)

    assert ctrl2.state == SystemState.DRAINING
