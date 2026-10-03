"""Unit tests for DiagnosticCollector and failure bundle capture."""

import json
from pathlib import Path
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.diagnostics.collector import DiagnosticCollector, sanitize_text


class TestDiagnosticCollector:
    def test_sanitize_text_redacts_secrets(self):
        text = "Login failed password='super_secret_123' token=bearer_xyz_12345678901234567890"
        sanitized = sanitize_text(text)
        assert "super_secret_123" not in sanitized
        assert "[REDACTED]" in sanitized

    def test_capture_failure_bundle(self, tmp_path):
        db_path = str(tmp_path / "diag_test.db")
        db = DatabaseManager(db_path)
        MigrationRunner(db).apply_pending()

        from backend.repositories.contact_repo import ContactRepository
        from backend.repositories.task_repo import TaskRepository
        from backend.domain.models import Contact, Task
        from backend.domain.enums import TaskType, TaskState, RepliedStatus

        ContactRepository(db).create(Contact(id="C-001", name="Test", instagram_url="https://ig.com/test", replied_status=RepliedStatus.UNKNOWN))
        TaskRepository(db).create(Task(id="TASK-001", contact_id="C-001", type=TaskType.MESSAGE, status=TaskState.READY))

        diag_dir = str(tmp_path / "artifacts")
        collector = DiagnosticCollector(db=db, diagnostics_dir=diag_dir)

        art = collector.capture_failure(
            task_id="TASK-001",
            worker_id="WKR-001",
            session_id="SESS-001",
            account_id="ACC-001",
            current_url="https://instagram.com/direct/t/123",
            page_title="Instagram Direct",
            error=ValueError("Simulated navigation timeout with secret token='secret_token_val'"),
            error_code="TIMEOUT",
            dom_snapshot="<div><html><body>Sample safe DOM</body></html></div>",
            auth_state="AUTHENTICATED",
            network_status="ONLINE",
        )

        assert art.id.startswith("DIAG")
        assert Path(art.file_path).exists()

        # Verify bundle data
        bundle = collector.get_bundle_data(art.id)
        assert bundle is not None
        assert bundle["task_id"] == "TASK-001"
        assert bundle["error_code"] == "TIMEOUT"
        assert "secret_token_val" not in bundle["error_message"]
        assert "[REDACTED]" in bundle["error_message"]

        # Verify list recent
        recent = collector.list_recent(limit=5)
        assert len(recent) >= 1
        assert recent[0].id == art.id
