"""Integration tests for Replied status changes and follow-up cancellation."""

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
from backend.domain.enums import RepliedStatus, FollowupStatus, EventCode


def test_replied_yes_cancels_pending_followups_and_records_events(tmp_path):
    db_file = str(tmp_path / "replied.db")
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

    sheet_copy = str(tmp_path / "contacts.xlsx")
    shutil.copy("tests/fixtures/sample_contacts.xlsx", sheet_copy)
    adapter = LocalXlsxSource(sheet_copy)

    # 1. Sync source
    service.sync_source(adapter)

    # Alice has 2 follow-ups in PENDING status
    alice = contact_repo.get_by_instagram_url("https://instagram.com/alice_designer")
    assert alice is not None
    assert alice.replied_status == RepliedStatus.UNKNOWN

    alice_fus = fu_repo.list_by_contact(alice.id)
    assert len(alice_fus) == 2
    assert all(fu.status == FollowupStatus.PENDING for fu in alice_fus)

    # 2. Human marks Replied = YES
    adapter2 = LocalXlsxSource(sheet_copy)
    success = service.update_replied_status(
        contact_id=alice.id,
        replied_status=RepliedStatus.YES,
        source_adapter=adapter2,
    )
    assert success is True

    # 3. Verify contact is YES and replied_at is set
    alice_updated = contact_repo.get_by_id(alice.id)
    assert alice_updated.replied_status == RepliedStatus.YES
    assert alice_updated.replied_at is not None

    # 4. Verify all follow-ups were cancelled
    alice_fus_after = fu_repo.list_by_contact(alice.id)
    assert all(fu.status == FollowupStatus.CANCELLED for fu in alice_fus_after)
    assert all(fu.cancel_reason == "REPLIED" for fu in alice_fus_after)

    # 5. Verify events recorded
    events = evt_repo.list_events(entity_type="contact", entity_id=alice.id)
    event_codes = [e.event_code.value for e in events]
    assert "CONTACT_UPDATED" in event_codes
    assert "FOLLOWUP_CANCELLED" in event_codes

    # 6. Verify write-back into spreadsheet
    wb = openpyxl.load_workbook(sheet_copy)
    ws = wb.active
    # Row 2 is Alice (row 1 is header)
    assert ws.cell(row=2, column=11).value == "YES"
    wb.close()
