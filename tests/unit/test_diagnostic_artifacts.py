"""Unit tests for DiagnosticArtifactRepository and retention cleanup (Section 7)."""

import os
from datetime import datetime, timezone, timedelta
import pytest
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.task_repo import TaskRepository
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.diagnostic_artifact_repo import DiagnosticArtifactRepository
from backend.domain.models import DiagnosticArtifact, Task, Contact, utc_now_iso
from backend.domain.enums import DiagnosticType, TaskState, TaskType, RepliedStatus


@pytest.fixture
def diag_env(tmp_path):
    db_file = str(tmp_path / "diag.db")
    db = DatabaseManager(db_file)
    MigrationRunner(db).apply_pending()
    contact_repo = ContactRepository(db)
    task_repo = TaskRepository(db)
    diag_repo = DiagnosticArtifactRepository(db)

    contact = Contact(id="C-DIAG-1", name="Diag Contact", instagram_url="https://instagram.com/test", replied_status=RepliedStatus.UNKNOWN)
    contact_repo.create(contact)

    task = Task(id="T-DIAG-1", contact_id="C-DIAG-1", type=TaskType.MESSAGE, status=TaskState.READY)
    task_repo.create(task)

    return db, task_repo, diag_repo, tmp_path


def test_create_and_retrieve_diagnostic_artifact(diag_env):
    _, _, diag_repo, tmp_path = diag_env

    file_path = str(tmp_path / "screenshot.png")
    with open(file_path, "w") as f:
        f.write("fake screenshot")

    artifact = DiagnosticArtifact(
        id="DIAG-1",
        task_id="T-DIAG-1",
        worker_id="W-1",
        session_id="SESS-1",
        correlation_id="CORR-1",
        timestamp=utc_now_iso(),
        artifact_type=DiagnosticType.SCREENSHOT,
        file_path=file_path,
        page_url="https://instagram.com/direct",
        page_title="Direct Messages",
        error_code="NET_TIMEOUT",
        reason="Network timeout on send",
        retention_until=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
    )

    created = diag_repo.create(artifact)
    assert created.id == "DIAG-1"

    retrieved = diag_repo.get_by_id("DIAG-1")
    assert retrieved is not None
    assert retrieved.page_title == "Direct Messages"
    assert retrieved.artifact_type == DiagnosticType.SCREENSHOT

    by_task = diag_repo.list_by_task("T-DIAG-1")
    assert len(by_task) == 1
    assert by_task[0].id == "DIAG-1"


def test_delete_expired_cleans_db_and_filesystem(diag_env):
    _, _, diag_repo, tmp_path = diag_env

    # 1. Create expired artifact with real file
    expired_file = str(tmp_path / "expired_artifact.html")
    with open(expired_file, "w") as f:
        f.write("<html>expired</html>")

    past_date = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    art1 = DiagnosticArtifact(
        id="DIAG-EXPIRED",
        task_id="T-DIAG-1",
        worker_id="W-1",
        session_id="SESS-1",
        correlation_id="CORR-1",
        timestamp=past_date,
        artifact_type=DiagnosticType.HTML,
        file_path=expired_file,
        retention_until=past_date,
    )
    diag_repo.create(art1)

    # 2. Create active artifact with real file
    active_file = str(tmp_path / "active_artifact.png")
    with open(active_file, "w") as f:
        f.write("active binary")

    future_date = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    art2 = DiagnosticArtifact(
        id="DIAG-ACTIVE",
        task_id="T-DIAG-1",
        worker_id="W-1",
        session_id="SESS-1",
        correlation_id="CORR-1",
        timestamp=utc_now_iso(),
        artifact_type=DiagnosticType.SCREENSHOT,
        file_path=active_file,
        retention_until=future_date,
    )
    diag_repo.create(art2)

    assert os.path.exists(expired_file) is True
    assert os.path.exists(active_file) is True

    # 3. Clean expired artifacts
    deleted_count = diag_repo.delete_expired()
    assert deleted_count == 1

    # 4. Verify expired file was deleted from disk and DB
    assert os.path.exists(expired_file) is False
    assert diag_repo.get_by_id("DIAG-EXPIRED") is None

    # 5. Verify active artifact remains in disk and DB
    assert os.path.exists(active_file) is True
    assert diag_repo.get_by_id("DIAG-ACTIVE") is not None
