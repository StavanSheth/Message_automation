"""Unit tests for ApplicationControlService managing operational states and lifecycle commands."""

import pytest
from unittest.mock import MagicMock
from backend.domain.enums import SystemState, EventCode
from backend.application.control_service import ApplicationControlService, ALLOWED_STATE_TRANSITIONS
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.event_repo import EventRepository
from backend.config.settings import AppSettings


@pytest.fixture
def control_env(tmp_path):
    db_path = str(tmp_path / "test_control.db")
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()

    event_repo = EventRepository(db)
    lifecycle_mock = MagicMock()
    lifecycle_mock.startup_recovery.return_value = {"status": "ok"}
    scheduler_mock = MagicMock()
    worker_mgr_mock = MagicMock()
    worker_mgr_mock.active_count = 1

    svc = ApplicationControlService(
        lifecycle_manager=lifecycle_mock,
        scheduler=scheduler_mock,
        worker_manager=worker_mgr_mock,
        event_repo=event_repo,
    )
    return svc, lifecycle_mock, scheduler_mock, worker_mgr_mock, event_repo


def test_initial_state_is_stopped(control_env):
    svc, _, _, _, _ = control_env
    assert svc.state == SystemState.STOPPED


def test_start_transitions_to_running(control_env):
    svc, lifecycle_mock, scheduler_mock, _, event_repo = control_env
    res = svc.start()
    assert res["status"] == "ok"
    assert svc.state == SystemState.RUNNING
    lifecycle_mock.startup_recovery.assert_called_once()
    scheduler_mock.start.assert_called_once()


def test_pause_and_resume(control_env):
    svc, _, scheduler_mock, _, _ = control_env
    svc.start()
    assert svc.state == SystemState.RUNNING

    paused = svc.pause("maintenance")
    assert paused is True
    assert svc.state == SystemState.PAUSED
    scheduler_mock.pause.assert_called_once()

    resumed = svc.resume("maintenance complete")
    assert resumed is True
    assert svc.state == SystemState.RUNNING
    assert scheduler_mock.resume.call_count == 2


def test_drain_transition(control_env):
    svc, _, scheduler_mock, worker_mgr_mock, _ = control_env
    svc.start()
    drained = svc.drain("preparing for upgrade")
    assert drained is True
    assert svc.state == SystemState.DRAINING


def test_stop_calls_shutdown(control_env):
    svc, lifecycle_mock, _, _, _ = control_env
    svc.start()
    svc.stop("system shutdown")
    assert svc.state == SystemState.STOPPED
    lifecycle_mock.graceful_shutdown.assert_called_once()


def test_invalid_state_transitions(control_env):
    svc, _, _, _, _ = control_env
    # From STOPPED, cannot jump directly to RUNNING without STARTING
    assert svc._transition_to(SystemState.PAUSED) is False
    assert svc._transition_to(SystemState.DRAINING) is False
    assert svc.state == SystemState.STOPPED
