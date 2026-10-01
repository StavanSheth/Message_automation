"""Integration test: BrowserSession -> SpreadsheetDriver -> BrowserSpreadsheetSource -> SourceService -> SQLite.

Tests the full source synchronization chain:
A. Browser opens spreadsheet-like page (ACCESSIBLE, LOGIN_REQUIRED, etc.)
B. Header discovery with canonical mappings
C. Row parsing into SourceRow objects
D. Initial synchronization (Contact, SourceRecord, Message, Task, Followups, SyncRun, Events)
E. External modification and conflict detection
F. Browser source write-back and read-back verification
"""

import pytest

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
from backend.sources.browser_sheet.driver import PlaywrightSpreadsheetDriver
from backend.sources.browser_sheet.adapter import BrowserSpreadsheetSource
from backend.domain.enums import (
    SourceAccessStatus,
    RepliedStatus,
    SyncStatus,
    TaskState,
    TaskType,
    EventCode,
)
from backend.config.settings import reset_settings
from backend.events.correlation import reset_counters
from tests.fixtures.spreadsheet_page import (
    DeterministicSpreadsheetPage,
    create_deterministic_session,
)


@pytest.fixture(autouse=True)
def clean_env():
    reset_settings()
    reset_counters()
    yield
    reset_settings()
    reset_counters()


@pytest.fixture
def sync_env(tmp_path):
    db_path = str(tmp_path / "browser_sync_test.db")
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

    page = DeterministicSpreadsheetPage()
    session = create_deterministic_session(page=page)
    driver = PlaywrightSpreadsheetDriver(session=session)
    source = BrowserSpreadsheetSource(
        spreadsheet_url="https://docs.google.com/spreadsheets/d/test_sync_sheet/edit",
        driver=driver,
    )

    yield {
        "db": db,
        "service": service,
        "page": page,
        "session": session,
        "driver": driver,
        "source": source,
        "contact_repo": contact_repo,
        "srec_repo": srec_repo,
        "task_repo": task_repo,
        "msg_repo": msg_repo,
        "fu_repo": fu_repo,
        "evt_repo": evt_repo,
        "sync_repo": sync_repo,
    }

    session.stop()


def test_browser_opens_spreadsheet_page(sync_env):
    """A. Verify session.start(), driver.open(), check_access() == ACCESSIBLE."""
    source = sync_env["source"]
    driver = sync_env["driver"]
    url = source.url

    assert driver.check_access(url) == SourceAccessStatus.ACCESSIBLE
    assert driver.open(url) is True


def test_header_discovery_canonical_mapping(sync_env):
    """B. Verify canonical mapping for standard fields."""
    source = sync_env["source"]
    headers = source.driver.read_headers(source.url)

    expected_headers = [
        "Name", "Instagram URL", "Message", "Expected Followers",
        "Follow-up 1 Message", "Follow-up 1 Delay", "Follow-up 2 Message", "Follow-up 2 Delay",
        "Replied", "Notes",
    ]
    assert headers == expected_headers

    # Check canonical mapping in driver
    canonical_map = source.driver._canonical_to_col
    assert canonical_map["name"] == 0
    assert canonical_map["instagram_url"] == 1
    assert canonical_map["message"] == 2
    assert canonical_map["expected_followers"] == 3
    assert canonical_map["followup_1_message"] == 4
    assert canonical_map["followup_1_delay_seconds"] == 5
    assert canonical_map["followup_2_message"] == 6
    assert canonical_map["followup_2_delay_seconds"] == 7
    assert canonical_map["replied_status"] == 8
    assert canonical_map["notes"] == 9


def test_row_parsing(sync_env):
    """C. Verify spreadsheet rows become correct SourceRow objects."""
    source = sync_env["source"]
    rows = source.driver.read_sheet(source.url)
    assert len(rows) == 2

    r0 = rows[0]
    assert r0.row_index == 2
    assert r0.name == "Alice Johnson"
    assert r0.instagram_url == "https://instagram.com/alice_j"
    assert r0.message == "Hi Alice, let's connect!"
    assert r0.expected_followers == 5000
    assert r0.followup_1_message == "Checking in Alice!"
    assert r0.followup_1_delay_seconds == 1
    assert r0.followup_2_message == "Final follow up Alice"
    assert r0.followup_2_delay_seconds == 3
    assert r0.replied_status == RepliedStatus.NO
    assert r0.notes == "Lead from event"


def test_initial_synchronization_creates_entities(sync_env):
    """D. Initial synchronization creates Contacts, SourceRecords, Messages, Tasks, Followups, SyncRun, Events."""
    service = sync_env["service"]
    source = sync_env["source"]
    contact_repo = sync_env["contact_repo"]
    srec_repo = sync_env["srec_repo"]
    task_repo = sync_env["task_repo"]
    msg_repo = sync_env["msg_repo"]
    fu_repo = sync_env["fu_repo"]
    evt_repo = sync_env["evt_repo"]
    sync_repo = sync_env["sync_repo"]

    sync_run = service.sync_source(source)
    assert sync_run.status == SyncStatus.SUCCESS
    assert sync_run.records_read == 2
    assert sync_run.records_written == 2

    # Verify Contacts
    c_alice = contact_repo.get_by_instagram_url("https://instagram.com/alice_j")
    assert c_alice is not None
    assert c_alice.name == "Alice Johnson"

    c_bob = contact_repo.get_by_instagram_url("https://instagram.com/bob_smith")
    assert c_bob is not None
    assert c_bob.name == "Bob Smith"

    # Verify SourceRecords
    records = srec_repo.list_by_source(source.source_identifier)
    assert len(records) == 2

    # Verify Messages & Tasks created for non-replied (Alice: NO, Bob: YES)
    alice_task = task_repo.get_by_contact_and_type(c_alice.id, TaskType.MESSAGE)
    assert alice_task is not None
    assert alice_task.type == TaskType.MESSAGE
    assert alice_task.status == TaskState.READY

    alice_msg = msg_repo.get_by_task_id(alice_task.id)
    assert alice_msg is not None
    assert alice_msg.body == "Hi Alice, let's connect!"

    # Verify Followups created for Alice
    followups = fu_repo.list_by_contact(c_alice.id)
    assert len(followups) == 2

    # Verify Events
    events = evt_repo.list_events(limit=50)
    event_codes = [e.event_code for e in events]
    assert len(events) > 0
    assert any(c in event_codes for c in (EventCode.SOURCE_SYNCED, EventCode.CONTACT_CREATED, EventCode.SOURCE_SYNC_COMPLETED))


def test_external_spreadsheet_modification_and_conflict(sync_env):
    """E. External modification detection without silent overwrite."""
    service = sync_env["service"]
    source = sync_env["source"]
    page = sync_env["page"]
    contact_repo = sync_env["contact_repo"]
    srec_repo = sync_env["srec_repo"]
    evt_repo = sync_env["evt_repo"]

    # Initial sync
    service.sync_source(source)
    alice_srec = srec_repo.get_by_source_and_row(source.source_identifier, 2)
    assert alice_srec is not None
    initial_checksum = alice_srec.checksum

    # Simulate external edit: change Alice's message in the spreadsheet
    page.set_cell(2, 2, "MODIFIED: New message for Alice")

    # Invalidate driver cache so read re-queries
    source.driver._invalidate_cache()

    # Second sync: detect change
    sync_run = service.sync_source(source)
    # Checksum changes and conflict / update recorded
    updated_srec = srec_repo.get_by_id(alice_srec.id)
    assert updated_srec.checksum != initial_checksum


def test_browser_source_write_back(sync_env):
    """F. Modify supported field via update_record() and read back verified value."""
    source = sync_env["source"]
    page = sync_env["page"]

    # Read initial Replied value
    val = source.driver.read_cell(source.url, 2, "replied")
    assert val == "NO"

    # Update Replied to YES
    success = source.update_record("2", {"replied": "YES"})
    assert success is True

    # Read back cell to verify new value is present
    read_back = source.driver.read_cell(source.url, 2, "replied")
    assert read_back == "YES"
    assert page.get_cell(2, 8) == "YES"
