"""Integration tests for Excel synchronization and spreadsheet conflict detection."""

import shutil
import pytest
import openpyxl
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.source_record_repo import SourceRecordRepository
from backend.repositories.task_repo import TaskRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.followup_repo import FollowupRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.sync_run_repo import SyncRunRepository
from backend.sources.xlsx.adapter import LocalXlsxSource
from backend.application.source_service import SourceService
from backend.domain.enums import SyncStatus, TaskType, TaskState, FollowupStatus


@pytest.fixture
def service_and_sheet(tmp_path):
    db_file = str(tmp_path / "sync.db")
    db = DatabaseManager(db_file)
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

    # Copy fixture to tmp_path so it can be safely modified
    sheet_copy = str(tmp_path / "contacts.xlsx")
    shutil.copy("tests/fixtures/sample_contacts.xlsx", sheet_copy)

    return service, sheet_copy, db


def test_initial_sync_creates_entities(service_and_sheet):
    service, sheet_path, db = service_and_sheet
    adapter = LocalXlsxSource(sheet_path)

    sync_run = service.sync_source(adapter)
    assert sync_run.status == SyncStatus.SUCCESS
    assert sync_run.records_read == 3
    assert sync_run.records_written == 3
    assert sync_run.conflicts == 0

    # Verify contacts created
    contact_repo = ContactRepository(db)
    contacts = contact_repo.list_all()
    assert len(contacts) == 3

    # Verify Alice's tasks and followups
    alice = contact_repo.get_by_instagram_url("https://instagram.com/alice_designer")
    assert alice is not None

    task_repo = TaskRepository(db)
    msg_task = task_repo.get_by_contact_and_type(alice.id, TaskType.MESSAGE, sequence=0)
    assert msg_task is not None
    assert msg_task.status == TaskState.READY

    fu_repo = FollowupRepository(db)
    alice_fus = fu_repo.list_by_contact(alice.id)
    assert len(alice_fus) == 2  # Follow-up 1 and Follow-up 2 configured for Alice


def test_conflict_detection_when_sheet_edited_externally(service_and_sheet):
    service, sheet_path, db = service_and_sheet
    adapter = LocalXlsxSource(sheet_path)

    # 1. First sync
    run1 = service.sync_source(adapter)
    assert run1.status == SyncStatus.SUCCESS
    assert run1.conflicts == 0

    # 2. Simulate external human edit in Excel sheet (e.g. Alice's follower count changed)
    wb = openpyxl.load_workbook(sheet_path)
    ws = wb.active
    ws.cell(row=2, column=5, value=99999)  # Alice's Expected Followers changed
    wb.save(sheet_path)
    wb.close()

    # 3. Second sync
    adapter2 = LocalXlsxSource(sheet_path)
    run2 = service.sync_source(adapter2)

    # Conflict must be detected because incoming row checksum changed
    assert run2.status == SyncStatus.CONFLICT
    assert run2.conflicts == 1

    # Verify conflict event was recorded
    evt_repo = EventRepository(db)
    events = evt_repo.list_events(category="source")
    assert any(e.event_code.value == "SOURCE_CONFLICT_DETECTED" for e in events)
