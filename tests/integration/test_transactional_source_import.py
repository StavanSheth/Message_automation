"""Integration tests for transactional coordination and atomicity during source import.

Verifies that:
- Coordinated creation of Contact + SourceRecord + Task + Message + Followups + Event succeeds atomically.
- Any mid-import failure (e.g. task creation error, message creation error, followup error) rolls back
  the entire row's changes, leaving NO orphaned/half-imported state in the database.
- Multi-row imports maintain isolation: a subsequent row failure rolls back that row without corrupting
  previously committed rows.
"""

from typing import List, Dict, Any
import pytest
from unittest.mock import MagicMock

from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.source_record_repo import SourceRecordRepository
from backend.repositories.task_repo import TaskRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.followup_repo import FollowupRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.sync_run_repo import SyncRunRepository
from backend.sources.base import SourceAdapter
from backend.application.source_service import SourceService
from backend.domain.models import SourceRow
from backend.domain.enums import SourceType, SourceAccessStatus, RepliedStatus, SyncStatus


class MockAdapter(SourceAdapter):
    """Controlled source adapter for transactional testing."""

    def __init__(self, identifier: str, rows: List[SourceRow]):
        super().__init__(identifier, SourceType.LOCAL_XLSX)
        self._rows = rows

    def open(self) -> bool:
        self.is_open = True
        return True

    def validate_access(self) -> SourceAccessStatus:
        return SourceAccessStatus.ACCESSIBLE

    def read_records(self) -> List[SourceRow]:
        return self._rows

    def update_record(self, row_index: int, updates: Dict[str, Any]) -> bool:
        return True

    def close(self) -> None:
        self.is_open = False


@pytest.fixture
def test_env(tmp_path):
    db_path = str(tmp_path / "transactional_import.db")
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()

    contact_repo = ContactRepository(db)
    srec_repo = SourceRecordRepository(db)
    task_repo = TaskRepository(db)
    msg_repo = MessageRepository(db)
    fu_repo = FollowupRepository(db)
    evt_repo = EventRepository(db)
    sync_repo = SyncRunRepository(db)

    service = SourceService(
        contact_repo=contact_repo,
        source_record_repo=srec_repo,
        task_repo=task_repo,
        message_repo=msg_repo,
        followup_repo=fu_repo,
        event_repo=evt_repo,
        sync_run_repo=sync_repo,
    )

    return {
        "db": db,
        "service": service,
        "contact_repo": contact_repo,
        "srec_repo": srec_repo,
        "task_repo": task_repo,
        "msg_repo": msg_repo,
        "fu_repo": fu_repo,
        "evt_repo": evt_repo,
        "sync_repo": sync_repo,
    }


def _make_row(index: int, username: str) -> SourceRow:
    return SourceRow(
        row_index=index,
        name=f"User {username}",
        instagram_url=f"https://instagram.com/{username}",
        username=username,
        message=f"Hello {username}, nice to meet you!",
        replied_status=RepliedStatus.UNKNOWN,
        followup_1_message="Following up on my message",
        followup_1_delay_seconds=86400,
        followup_2_message="Second follow up",
        followup_2_delay_seconds=172800,
        checksum=f"chk_{username}_{index}",
        raw_values={"Name": f"User {username}", "Instagram": f"https://instagram.com/{username}"},
    )


def test_full_coordinated_import_success(test_env):
    """Verify that a successful row creates all operational records coordinated together."""
    service = test_env["service"]
    row = _make_row(1, "alice")
    adapter = MockAdapter("test_source_1", [row])

    sync_run = service.sync_source(adapter)
    assert sync_run.status == SyncStatus.SUCCESS
    assert sync_run.records_written == 1

    # Verify contact
    contact = test_env["contact_repo"].get_by_instagram_url(row.instagram_url)
    assert contact is not None
    assert contact.username == "alice"

    # Verify source record
    srec = test_env["srec_repo"].get_by_source_and_row(adapter.source_identifier, 1)
    assert srec is not None
    assert srec.contact_id == contact.id
    assert contact.source_record_id == srec.id

    # Verify initial task and message
    tasks = test_env["task_repo"].get_by_contact_id(contact.id)
    assert len(tasks) == 1
    assert tasks[0].contact_id == contact.id

    messages = test_env["msg_repo"].list_by_contact(contact.id)
    assert len(messages) == 1
    assert messages[0].body == "Hello alice, nice to meet you!"
    assert messages[0].task_id == tasks[0].id

    # Verify followups
    fus = test_env["fu_repo"].list_by_contact(contact.id)
    assert len(fus) == 2
    assert fus[0].sequence == 1
    assert fus[1].sequence == 2


def test_rollback_on_task_creation_failure(test_env):
    """Simulate task creation failure and verify complete rollback with no orphaned contact or source_record."""
    service = test_env["service"]
    task_repo = test_env["task_repo"]
    row = _make_row(2, "bob")
    adapter = MockAdapter("test_source_2", [row])

    # Monkeypatch task_repo.create to raise an unexpected database error
    orig_create = task_repo.create

    def fail_create(t):
        raise RuntimeError("Simulated failure in TaskRepository.create")

    task_repo.create = fail_create

    with pytest.raises(RuntimeError, match="Simulated failure in TaskRepository.create"):
        service.sync_source(adapter)

    # Verify atomic rollback: Contact and SourceRecord must NOT exist
    contact = test_env["contact_repo"].get_by_instagram_url(row.instagram_url)
    assert contact is None

    srec = test_env["srec_repo"].get_by_source_and_row(adapter.source_identifier, 2)
    assert srec is None

    # Tasks and messages must not exist
    conn = test_env["db"].get_connection()
    c = conn.execute("SELECT COUNT(*) FROM tasks;").fetchone()[0]
    assert c == 0
    m = conn.execute("SELECT COUNT(*) FROM messages;").fetchone()[0]
    assert m == 0


def test_rollback_on_message_creation_failure(test_env):
    """Simulate message creation failure and verify rollback of Contact, SourceRecord, and Task."""
    service = test_env["service"]
    msg_repo = test_env["msg_repo"]
    row = _make_row(3, "charlie")
    adapter = MockAdapter("test_source_3", [row])

    def fail_msg_create(m):
        raise RuntimeError("Simulated failure in MessageRepository.create")

    msg_repo.create = fail_msg_create

    with pytest.raises(RuntimeError, match="Simulated failure in MessageRepository.create"):
        service.sync_source(adapter)

    # Everything must be rolled back
    assert test_env["contact_repo"].get_by_instagram_url(row.instagram_url) is None
    assert test_env["srec_repo"].get_by_source_and_row(adapter.source_identifier, 3) is None

    conn = test_env["db"].get_connection()
    assert conn.execute("SELECT COUNT(*) FROM tasks;").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM messages;").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM followups;").fetchone()[0] == 0


def test_rollback_on_followup_creation_failure(test_env):
    """Simulate failure during followup creation (e.g. sequence 2 failure) and verify atomicity."""
    service = test_env["service"]
    fu_repo = test_env["fu_repo"]
    row = _make_row(4, "diana")
    adapter = MockAdapter("test_source_4", [row])

    orig_fu_create = fu_repo.create

    def fail_fu_create(fu):
        if fu.sequence == 2:
            raise RuntimeError("Simulated failure on Followup sequence 2")
        return orig_fu_create(fu)

    fu_repo.create = fail_fu_create

    with pytest.raises(RuntimeError, match="Simulated failure on Followup sequence 2"):
        service.sync_source(adapter)

    # Complete rollback
    assert test_env["contact_repo"].get_by_instagram_url(row.instagram_url) is None
    assert test_env["srec_repo"].get_by_source_and_row(adapter.source_identifier, 4) is None

    conn = test_env["db"].get_connection()
    assert conn.execute("SELECT COUNT(*) FROM tasks;").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM messages;").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM followups;").fetchone()[0] == 0


def test_multi_row_isolation_on_subsequent_failure(test_env):
    """Verify that if row 1 succeeds and row 2 fails, row 1 remains intact while row 2 is rolled back."""
    service = test_env["service"]
    row1 = _make_row(1, "edward")
    row2 = _make_row(2, "fiona")
    adapter = MockAdapter("test_source_5", [row1, row2])

    task_repo = test_env["task_repo"]
    orig_create = task_repo.create

    def fail_on_fiona(task):
        # Find which contact this is
        contact = test_env["contact_repo"].get_by_id(task.contact_id)
        if contact and contact.username == "fiona":
            raise RuntimeError("Simulated crash on fiona's task")
        return orig_create(task)

    task_repo.create = fail_on_fiona

    with pytest.raises(RuntimeError, match="Simulated crash on fiona's task"):
        service.sync_source(adapter)

    # Row 1 (Edward) MUST exist and be completely intact
    edward_contact = test_env["contact_repo"].get_by_instagram_url(row1.instagram_url)
    assert edward_contact is not None
    assert edward_contact.username == "edward"
    assert test_env["srec_repo"].get_by_source_and_row(adapter.source_identifier, 1) is not None
    assert len(test_env["task_repo"].get_by_contact_id(edward_contact.id)) == 1
    assert len(test_env["fu_repo"].list_by_contact(edward_contact.id)) == 2

    # Row 2 (Fiona) MUST NOT exist (no partial state)
    assert test_env["contact_repo"].get_by_instagram_url(row2.instagram_url) is None
    assert test_env["srec_repo"].get_by_source_and_row(adapter.source_identifier, 2) is None
    conn = test_env["db"].get_connection()
    fiona_tasks = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE contact_id NOT IN (SELECT id FROM contacts);"
    ).fetchone()[0]
    assert fiona_tasks == 0
