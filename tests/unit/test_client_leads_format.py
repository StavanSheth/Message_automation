"""Tests for client leads spreadsheet column format and status mapping."""

import openpyxl
import pytest

from backend.domain.enums import TaskState, MessageState, RepliedStatus
from backend.domain.models import SourceRow
from backend.sources.browser_sheet.spreadsheet import SpreadsheetStructureValidator
from backend.sources.xlsx.adapter import LocalXlsxSource
from backend.application.source_service import SourceService
from backend.database.manager import DatabaseManager
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.source_record_repo import SourceRecordRepository
from backend.repositories.task_repo import TaskRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.followup_repo import FollowupRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.sync_run_repo import SyncRunRepository


SAMPLE_ROWS = [
    ["Id No", "Client Name", "Business Industry", "Google maps Link", "Website Link", "Instagram id/ link", "Remarks", "Follow Up 1"],
    ["174", "Incotex", "Menswear", "https://maps.google.com/?q=incotex", "https://www.incotex.com", "https://www.instagram.com/incotex", "Done", "Done"],
    ["175", "Isca Foods", "Food retail", "https://maps.google.com/?q=isca", "0", "https://www.instagram.com/iscawines?utm_source=test", "Done", "Done"],
    ["183", "Junk", "Fashion", "", "", "https://www.instagram.com/junkmcr/", "Permanently Closed", "Not Applicable"],
    ["185", "KAAWA", "Local Independent", "", "", "https://www.instagram.com/kaawa_mcr/", "Not reachable", "Not Applicable"],
    ["196", "Las Iguanas Deansgate", "Latin restaurant", "https://maps.google.com/?q=iguanas", "https://www.iguanas.co.uk", "https://www.instagram.com/lasiguanas", "Pending", ""],
    ["199", "Liberty London Pop-up", "Department store", "https://maps.google.com/?q=liberty", "https://www.libertylondon.com", "https://www.instagram.com/libertylondon", "Pending", ""],
    ["200", "Liquor & Burn", "Barbershop", "https://maps.google.com/?q=liquor", "https://www.liquorandburn.com", "https://www.instagram.com/liquorandburn", "Pending", ""],
]


def test_validator_with_client_columns():
    headers = SAMPLE_ROWS[0]
    canonical_map, col_map = SpreadsheetStructureValidator.validate_headers(headers)
    assert canonical_map["contact_id"] == 0
    assert canonical_map["name"] == 1
    assert canonical_map["industry"] == 2
    assert canonical_map["google_maps_link"] == 3
    assert canonical_map["website_link"] == 4
    assert canonical_map["instagram_url"] == 5
    assert canonical_map["remarks"] == 6
    assert canonical_map["followup_1"] == 7

    row196 = SpreadsheetStructureValidator.parse_row(
        row_cells=SAMPLE_ROWS[5],
        row_index=6,
        idx_to_canonical=col_map,
    )
    assert row196 is not None
    assert row196.contact_id == "196"
    assert row196.name == "Las Iguanas Deansgate"
    assert row196.instagram_url == "https://www.instagram.com/lasiguanas"
    assert "Pending" in (row196.notes or "")
    assert "Latin restaurant" in (row196.notes or "")
    assert row196.message == "Hello Las Iguanas Deansgate, hope you are doing well!"


def test_xlsx_adapter_and_sync_with_client_columns(tmp_path):
    # 1. Create test Excel file
    xlsx_file = str(tmp_path / "client_leads.xlsx")
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in SAMPLE_ROWS:
        ws.append(row)
    wb.save(xlsx_file)
    wb.close()

    adapter = LocalXlsxSource(xlsx_file)
    records = adapter.read_records()
    assert len(records) == 7

    # 2. Setup in-memory Database and Repositories
    from backend.database.migrations import MigrationRunner
    db_file = str(tmp_path / "test.db")
    db = DatabaseManager(db_file)
    MigrationRunner(db).apply_pending()

    contact_repo = ContactRepository(db)
    source_record_repo = SourceRecordRepository(db)
    task_repo = TaskRepository(db)
    message_repo = MessageRepository(db)
    followup_repo = FollowupRepository(db)
    event_repo = EventRepository(db)
    sync_run_repo = SyncRunRepository(db)

    service = SourceService(
        contact_repo=contact_repo,
        source_record_repo=source_record_repo,
        task_repo=task_repo,
        message_repo=message_repo,
        followup_repo=followup_repo,
        event_repo=event_repo,
        sync_run_repo=sync_run_repo,
    )

    sync_run = service.sync_source(adapter)
    assert sync_run.records_read == 7

    # 3. Verify task statuses:
    # Incotex (Done) -> COMPLETED
    incotex = contact_repo.get_by_id("174")
    assert incotex is not None
    incotex_task = task_repo.get_by_contact_and_type("174", "MESSAGE", 0)
    assert incotex_task.status == TaskState.COMPLETED

    # Junk (Permanently Closed) -> SKIPPED
    junk_task = task_repo.get_by_contact_and_type("183", "MESSAGE", 0)
    assert junk_task.status == TaskState.SKIPPED

    # KAAWA (Not reachable) -> SKIPPED
    kaawa_task = task_repo.get_by_contact_and_type("185", "MESSAGE", 0)
    assert kaawa_task.status == TaskState.SKIPPED

    # Las Iguanas Deansgate (Pending) -> READY
    iguanas_task = task_repo.get_by_contact_and_type("196", "MESSAGE", 0)
    assert iguanas_task.status == TaskState.READY

    # Liberty London (Pending) -> READY
    liberty_task = task_repo.get_by_contact_and_type("199", "MESSAGE", 0)
    assert liberty_task.status == TaskState.READY

    # Liquor & Burn (Pending) -> READY
    liquor_task = task_repo.get_by_contact_and_type("200", "MESSAGE", 0)
    assert liquor_task.status == TaskState.READY

    db.close()
