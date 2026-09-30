"""Comprehensive end-to-end integration tests for Workflows A through F."""

import shutil
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
from backend.sources.xlsx.adapter import LocalXlsxSource
from backend.application.source_service import SourceService
from backend.application.task_service import TaskService
from backend.recovery.service import DefaultRecoveryService
from backend.domain.enums import TaskType, TaskState, SyncStatus, RepliedStatus, FollowupStatus


@pytest.fixture
def environment(tmp_path):
    db_file = str(tmp_path / "workflows.db")
    db = DatabaseManager(db_file)
    MigrationRunner(db).apply_pending()

    contact_repo = ContactRepository(db)
    srec_repo = SourceRecordRepository(db)
    task_repo = TaskRepository(db)
    msg_repo = MessageRepository(db)
    fu_repo = FollowupRepository(db)
    evt_repo = EventRepository(db)
    sync_repo = SyncRunRepository(db)

    source_service = SourceService(
        contact_repo=contact_repo,
        source_record_repo=srec_repo,
        task_repo=task_repo,
        message_repo=msg_repo,
        followup_repo=fu_repo,
        event_repo=evt_repo,
        sync_run_repo=sync_repo,
    )

    task_service = TaskService(task_repo, evt_repo)
    recovery_service = DefaultRecoveryService(task_repo, evt_repo)

    sheet_copy = str(tmp_path / "sheet.xlsx")
    shutil.copy("tests/fixtures/sample_contacts.xlsx", sheet_copy)

    return {
        "db": db,
        "contact_repo": contact_repo,
        "task_repo": task_repo,
        "fu_repo": fu_repo,
        "source_service": source_service,
        "task_service": task_service,
        "recovery_service": recovery_service,
        "sheet_path": sheet_copy,
    }


def test_workflow_a_and_b_xlsx_ingestion_and_duplicate_sync(environment):
    """
    Workflow A: XLSX -> validate -> parse -> checksum -> source record -> contact -> task
    Workflow B: same XLSX -> second sync -> no duplicate contact -> no duplicate task
    """
    env = environment
    service: SourceService = env["source_service"]
    contact_repo: ContactRepository = env["contact_repo"]
    task_repo: TaskRepository = env["task_repo"]
    adapter = LocalXlsxSource(env["sheet_path"])

    # First Sync (Workflow A)
    run1 = service.sync_source(adapter)
    assert run1.status == SyncStatus.SUCCESS
    assert run1.records_read == 3
    assert run1.records_written == 3
    assert contact_repo.count() == 3

    initial_tasks = task_repo.list_ready()
    assert len(initial_tasks) == 3

    # Second Sync of identical sheet (Workflow B)
    adapter2 = LocalXlsxSource(env["sheet_path"])
    run2 = service.sync_source(adapter2)
    assert run2.status == SyncStatus.SUCCESS
    assert run2.records_read == 3
    assert run2.records_written == 0  # No new records written
    assert run2.conflicts == 0

    # No duplicate contacts or tasks created
    assert contact_repo.count() == 3
    assert len(task_repo.list_ready()) == 3


def test_workflow_c_task_lifecycle(environment):
    """
    Workflow C: CREATED -> VALIDATING -> QUEUED -> READY -> RUNNING -> COMPLETED
    """
    env = environment
    task_service: TaskService = env["task_service"]
    contact_repo: ContactRepository = env["contact_repo"]

    contact = contact_repo.create(
        contact_repo._row_to_contact({
            "id": "C-LIFE",
            "source_record_id": None,
            "name": "Lifecycle User",
            "instagram_url": "https://instagram.com/life",
            "username": "life",
            "expected_followers": 100,
            "notes": None,
            "replied_status": "UNKNOWN",
            "replied_source": "MANUAL",
            "replied_at": None,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        })
    )

    task = task_service.create_task(contact_id=contact.id, task_type=TaskType.MESSAGE)
    assert task.status == TaskState.CREATED

    t = task_service.transition_state(task.id, TaskState.VALIDATING)
    assert t.status == TaskState.VALIDATING

    t = task_service.transition_state(task.id, TaskState.QUEUED)
    assert t.status == TaskState.QUEUED

    t = task_service.transition_state(task.id, TaskState.READY)
    assert t.status == TaskState.READY

    claimed = task_service.claim_task(task.id, worker_id="WORKER-1", lock_token="TOKEN-1")
    assert claimed is True
    t = env["task_repo"].get_by_id(task.id)
    assert t.status == TaskState.RUNNING

    t = task_service.transition_state(task.id, TaskState.COMPLETED)
    assert t.status == TaskState.COMPLETED


def test_workflow_d_failed_task_retry(environment):
    """
    Workflow D: RUNNING -> RETRY_WAIT -> READY
    """
    env = environment
    task_service: TaskService = env["task_service"]
    contact_repo: ContactRepository = env["contact_repo"]

    contact = contact_repo.create(
        contact_repo._row_to_contact({
            "id": "C-RETRY",
            "source_record_id": None,
            "name": "Retry User",
            "instagram_url": "https://instagram.com/retry",
            "username": "retry",
            "expected_followers": 200,
            "notes": None,
            "replied_status": "UNKNOWN",
            "replied_source": "MANUAL",
            "replied_at": None,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        })
    )

    task = task_service.create_task(contact_id=contact.id, task_type=TaskType.MESSAGE)
    task_service.transition_state(task.id, TaskState.VALIDATING)
    task_service.transition_state(task.id, TaskState.QUEUED)
    task_service.transition_state(task.id, TaskState.READY)
    task_service.claim_task(task.id, worker_id="W1", lock_token="T1")

    # Transient network failure moves task from RUNNING to RETRY_WAIT
    t = task_service.transition_state(task.id, TaskState.RETRY_WAIT)
    assert t.status == TaskState.RETRY_WAIT

    # After backoff, task becomes READY again
    t_ready = task_service.transition_state(task.id, TaskState.READY)
    assert t_ready.status == TaskState.READY


def test_workflow_e_crash_recovery(environment):
    """
    Workflow E: RUNNING -> application restart -> INTERRUPTED -> recovery path (RECONCILING -> READY)
    """
    env = environment
    task_service: TaskService = env["task_service"]
    recovery_service: DefaultRecoveryService = env["recovery_service"]
    contact_repo: ContactRepository = env["contact_repo"]

    contact = contact_repo.create(
        contact_repo._row_to_contact({
            "id": "C-CRASH-WF",
            "source_record_id": None,
            "name": "Crash User",
            "instagram_url": "https://instagram.com/crash_wf",
            "username": "crash",
            "expected_followers": 300,
            "notes": None,
            "replied_status": "UNKNOWN",
            "replied_source": "MANUAL",
            "replied_at": None,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        })
    )

    task = task_service.create_task(contact_id=contact.id, task_type=TaskType.MESSAGE)
    task_service.transition_state(task.id, TaskState.VALIDATING)
    task_service.transition_state(task.id, TaskState.QUEUED)
    task_service.transition_state(task.id, TaskState.READY)
    task_service.claim_task(task.id, worker_id="W1", lock_token="T1")

    # 1. Restart happens: RUNNING becomes INTERRUPTED
    interrupted_count = task_service.recover_interrupted_on_startup()
    assert interrupted_count >= 1

    t = env["task_repo"].get_by_id(task.id)
    assert t.status == TaskState.INTERRUPTED

    # 2. Recovery path: reconcile interrupted tasks to READY
    reconciled = recovery_service.reconcile_interrupted(action="READY")
    assert any(t.id == task.id and t.status == TaskState.READY for t in reconciled)


def test_workflow_f_replied_contact(environment):
    """
    Workflow F: message completed -> contact replied YES -> pending follow-ups cancelled
    """
    env = environment
    source_service: SourceService = env["source_service"]
    contact_repo: ContactRepository = env["contact_repo"]
    fu_repo: FollowupRepository = env["fu_repo"]
    adapter = LocalXlsxSource(env["sheet_path"])

    source_service.sync_source(adapter)

    # Bob has a pending Follow-up 1
    bob = contact_repo.get_by_instagram_url("https://instagram.com/bobjones_photo")
    bob_fus = fu_repo.list_by_contact(bob.id)
    assert len(bob_fus) >= 1
    assert any(fu.status == FollowupStatus.PENDING for fu in bob_fus)

    # User updates Bob's status to YES
    adapter2 = LocalXlsxSource(env["sheet_path"])
    source_service.update_replied_status(bob.id, RepliedStatus.YES, source_adapter=adapter2)

    # Verify all followups cancelled
    bob_fus_after = fu_repo.list_by_contact(bob.id)
    assert all(fu.status == FollowupStatus.CANCELLED for fu in bob_fus_after)
    assert all(fu.cancel_reason == "REPLIED" for fu in bob_fus_after)
