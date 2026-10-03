"""
Tests for lead remarks updating and spreadsheet write-back verification.
"""

import json
import pytest
from unittest.mock import MagicMock
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.domain.models import Contact, Task, Message, SourceRecord, utc_now_iso
from backend.domain.enums import TaskState, TaskType, MessageState, SourceType
from backend.application.source_service import SourceService
from backend.application.execution_service import ExecutionService


@pytest.fixture
def db(tmp_path):
    mgr = DatabaseManager(str(tmp_path / "test_wb.db"))
    MigrationRunner(mgr).apply_pending()
    return mgr


def test_source_service_update_lead_remarks_updates_db_and_adapter(db):
    from backend.repositories.contact_repo import ContactRepository
    from backend.repositories.source_record_repo import SourceRecordRepository
    from backend.repositories.task_repo import TaskRepository
    from backend.repositories.message_repo import MessageRepository
    from backend.repositories.sync_run_repo import SyncRunRepository
    from backend.repositories.event_repo import EventRepository
    from backend.repositories.followup_repo import FollowupRepository

    c_repo = ContactRepository(db)
    srec_repo = SourceRecordRepository(db)

    # 1. Create contact first (due to FK)
    contact = Contact(
        id="CID-WB-1",
        source_record_id=None,
        name="Test Lead",
        instagram_url="https://www.instagram.com/test_lead/",
        notes=json.dumps({"industry": "Fashion", "remarks": "Pending"}),
    )
    c_repo.create(contact)

    # 2. Create source record referencing contact
    srec = SourceRecord(
        id="SR-WB-1",
        source_type=SourceType.LOCAL_XLSX,
        source_identifier="test_leads.xlsx",
        row_index=2,
        raw_data_json="{}",
        checksum="chk-123",
        last_synced_at=utc_now_iso(),
        contact_id="CID-WB-1",
    )
    srec_repo.create(srec)

    # Link contact to source record
    contact.source_record_id = srec.id
    c_repo.update(contact)

    svc = SourceService(
        contact_repo=c_repo,
        source_record_repo=srec_repo,
        task_repo=TaskRepository(db),
        message_repo=MessageRepository(db),
        sync_run_repo=SyncRunRepository(db),
        event_repo=EventRepository(db),
        followup_repo=FollowupRepository(db),
    )

    mock_adapter = MagicMock()
    success = svc.update_lead_remarks(
        contact_id="CID-WB-1",
        remarks="Done",
        source_adapter=mock_adapter,
    )

    assert success is True

    # Verify contact in DB updated
    updated_c = c_repo.get_by_id("CID-WB-1")
    parsed_notes = json.loads(updated_c.notes)
    assert parsed_notes["remarks"] == "Done"

    # Verify adapter write-back called
    mock_adapter.update_record.assert_called_once_with(2, {"remarks": "Done"})


def test_execution_service_auto_updates_lead_remarks_on_success(db):
    from backend.repositories.task_repo import TaskRepository
    from backend.repositories.message_repo import MessageRepository
    from backend.repositories.contact_repo import ContactRepository
    from backend.repositories.source_record_repo import SourceRecordRepository

    t_repo = TaskRepository(db)
    m_repo = MessageRepository(db)
    c_repo = ContactRepository(db)
    srec_repo = SourceRecordRepository(db)

    # Create contact
    contact = Contact(
        id="CID-AUTO-1",
        name="Auto Lead",
        instagram_url="https://www.instagram.com/autolead/",
        notes=json.dumps({"industry": "Tech", "remarks": "Pending"}),
    )
    c_repo.create(contact)

    # Create task
    task = Task(
        id="TASK-AUTO-1",
        contact_id="CID-AUTO-1",
        type=TaskType.MESSAGE,
        status=TaskState.READY,
    )
    t_repo.create(task)

    # Acquire lease for worker to satisfy Phase 3 invariant
    lease_id = t_repo.acquire_lease("TASK-AUTO-1", "WRK-AUTO-1", 120)
    assert lease_id is not None

    # Create message
    msg = Message(
        id="MSG-AUTO-1",
        task_id=task.id,
        contact_id=task.contact_id,
        body="Hello!",
        status=MessageState.PENDING,
    )
    m_repo.create(msg)

    # Mock Automation Service
    mock_auto = MagicMock()
    mock_auto.execute_messaging_task.return_value = True

    # Mock session
    mock_session = MagicMock()
    mock_session.session_id = "SES-AUTO-1"
    mock_session.auth_status = "AUTHENTICATED"
    mock_session.is_alive.return_value = True

    exec_svc = ExecutionService(
        task_repo=t_repo,
        message_repo=m_repo,
        automation_service=mock_auto,
        contact_repo=c_repo,
        source_record_repo=srec_repo,
    )

    res = exec_svc.execute_task(
        task_id=task.id,
        session=mock_session,
        worker_id="WRK-AUTO-1",
        lease_id=lease_id,
    )

    assert res is True

    # Verify task state is COMPLETED
    updated_t = t_repo.get_by_id(task.id)
    assert updated_t.status == TaskState.COMPLETED

    # Verify contact remarks in DB was updated to "Done"
    updated_c = c_repo.get_by_id("CID-AUTO-1")
    parsed_notes = json.loads(updated_c.notes)
    assert parsed_notes["remarks"] == "Done"
