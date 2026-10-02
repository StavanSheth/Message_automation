"""Phase 5 Integration Tests: Production Wiring and Dependency Graph Validation."""

import pytest
from backend.bootstrap.builder import build_production_app, ProductionApp
from backend.domain.enums import SystemState, TaskType
from backend.domain.models import Task, Contact
from tests.fixtures.mock_browser import create_mock_session


@pytest.fixture
def prod_env(tmp_path):
    db_file = str(tmp_path / "prod_test.db")
    app = build_production_app(db_path=db_file)
    return app, db_file


def test_complete_production_wiring_and_validation(prod_env):
    """Verify that build_production_app builds a fully intact, valid dependency graph."""
    app, _ = prod_env
    is_valid, errors = app.validate_dependency_graph()
    assert is_valid is True
    assert len(errors) == 0

    # Verify structural messaging dependency chain:
    # Scheduler -> TaskDispatcher -> WorkerManager -> TaskExecutor -> ExecutionService -> AutomationService
    assert app.scheduler.task_dispatcher is app.task_dispatcher
    assert app.worker_manager.execution_service is app.execution_service
    assert app.task_executor.execution_service is app.execution_service
    assert app.task_dispatcher.throttling_service is app.throttling_service
    assert app.execution_service.automation_service is app.automation_service
    assert app.execution_service.reconciliation_service is app.reconciliation_service


def test_missing_mandatory_dependency_rejects_running(tmp_path):
    """
    Section 5 invariant:
    If a mandatory component is missing:
    STARTING -> DEGRADED / STOPPED
    Never:
    STARTING -> RUNNING
    when critical dependencies failed.
    """
    db_file = str(tmp_path / "broken_prod.db")
    app = build_production_app(db_path=db_file)

    # Artificially sever ExecutionService from the graph
    app.execution_service = None

    is_valid, errors = app.validate_dependency_graph()
    assert is_valid is False
    assert any("ExecutionService" in err for err in errors)

    # Attempt to start must fail and route to DEGRADED, never RUNNING
    result = app.start()
    assert result["status"] == "error"
    assert app.control_service.state == SystemState.DEGRADED
    assert app.control_service.state != SystemState.RUNNING


def test_missing_task_dispatcher_rejects_running(tmp_path):
    """Verify missing TaskDispatcher causes start() failure and DEGRADED state."""
    db_file = str(tmp_path / "broken_dispatcher.db")
    app = build_production_app(db_path=db_file)

    app.task_dispatcher = None

    is_valid, errors = app.validate_dependency_graph()
    assert is_valid is False
    assert any("TaskDispatcher" in err for err in errors)

    result = app.start()
    assert result["status"] == "error"
    assert app.control_service.state == SystemState.DEGRADED
    assert app.control_service.state != SystemState.RUNNING
