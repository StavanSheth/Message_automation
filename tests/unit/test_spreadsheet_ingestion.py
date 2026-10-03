"""Unit and integration tests for SpreadsheetIngestionService and server import endpoint."""

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
from backend.application.source_service import SourceService
from backend.application.spreadsheet_ingestion import SpreadsheetIngestionService
from backend.domain.enums import TaskState

SAMPLE_DATA_TSV = (
    "Id No\tClient Name\tBusiness Industry\tGoogle maps Link\tWebsite Link\tInstagram id/ link\tRemarks\tFollow Up 1\n"
    "174\tIncotex\tMenswear\thttps://maps.google.com/1\thttps://incotex.com\thttps://www.instagram.com/incotex\tDone\tDone\n"
    "183\tJunk\tFashion\t\t\thttps://www.instagram.com/junkmcr/\tPermanently Closed\tNot Applicable\n"
    "196\tLas Iguanas Deansgate\tLatin restaurant\thttps://maps.google.com/2\thttps://iguanas.co.uk\thttps://www.instagram.com/lasiguanas\tPending\tPending\n"
)


def test_spreadsheet_ingestion_from_pasted_data(tmp_path):
    db = DatabaseManager(str(tmp_path / "test_ingest.db"))
    MigrationRunner(db).apply_pending()

    contact_repo = ContactRepository(db)
    source_record_repo = SourceRecordRepository(db)
    task_repo = TaskRepository(db)
    message_repo = MessageRepository(db)
    followup_repo = FollowupRepository(db)
    event_repo = EventRepository(db)
    sync_run_repo = SyncRunRepository(db)

    source_service = SourceService(
        contact_repo=contact_repo,
        source_record_repo=source_record_repo,
        task_repo=task_repo,
        message_repo=message_repo,
        followup_repo=followup_repo,
        event_repo=event_repo,
        sync_run_repo=sync_run_repo,
    )

    ingestion = SpreadsheetIngestionService(source_service=source_service)

    result = ingestion.import_from_input(
        source_input=SAMPLE_DATA_TSV,
        message_template="Hey {name}, loved your page!",
    )

    assert result["success"] is True
    assert result["records_read"] == 3
    assert result["ready"] == 1       # Las Iguanas Deansgate (Pending)
    assert result["completed"] == 1   # Incotex (Done)
    assert result["skipped"] == 1     # Junk (Permanently Closed)

    # Verify task states in database
    task_done = task_repo.get_by_contact_and_type("174", "MESSAGE", 0)
    assert task_done.status == TaskState.COMPLETED

    task_skipped = task_repo.get_by_contact_and_type("183", "MESSAGE", 0)
    assert task_skipped.status == TaskState.SKIPPED

    task_ready = task_repo.get_by_contact_and_type("196", "MESSAGE", 0)
    assert task_ready.status == TaskState.READY

    # Verify customized message template
    msg_ready = message_repo.get_by_task_id(task_ready.id)
    assert msg_ready.body == "Hey Las Iguanas Deansgate, loved your page!"

    db.close()
