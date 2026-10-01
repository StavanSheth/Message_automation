"""Phase 3 unit tests for Reconciliation Engine (Workstreams E, Q)."""

import pytest
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.task_repo import TaskRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.reconciliation_repo import ReconciliationRepository
from backend.repositories.manual_review_repo import ManualReviewRepository
from backend.repositories.event_repo import EventRepository
from backend.reconciliation.service import ReconciliationService
from backend.reconciliation.resolver import ReconciliationResolver
from backend.domain.models import Task, Message, Contact
from backend.domain.enums import TaskState, MessageState, TaskType, ReconciliationState, ReconciliationResolution


@pytest.fixture
def reconciliation_env(tmp_path):
    db = DatabaseManager(str(tmp_path / "test_reconciliation.db"))
    MigrationRunner(db).apply_pending()
    contact_repo = ContactRepository(db)
    task_repo = TaskRepository(db)
    message_repo = MessageRepository(db)
    reconciliation_repo = ReconciliationRepository(db)
    manual_review_repo = ManualReviewRepository(db)
    event_repo = EventRepository(db)

    # Seed contact for foreign key constraints
    contact = Contact(id="C-1", name="Test User", instagram_url="https://instagram.com/test_user")
    contact_repo.create(contact)

    resolver = ReconciliationResolver(
        task_repo=task_repo,
        message_repo=message_repo,
        event_repo=event_repo,
    )

    service = ReconciliationService(
        reconciliation_repo=reconciliation_repo,
        task_repo=task_repo,
        message_repo=message_repo,
        manual_review_repo=manual_review_repo,
        event_repo=event_repo,
        resolver=resolver,
    )

    return {
        "db": db,
        "task_repo": task_repo,
        "message_repo": message_repo,
        "reconciliation_repo": reconciliation_repo,
        "manual_review_repo": manual_review_repo,
        "service": service,
    }


def test_enter_reconciliation(reconciliation_env):
    task_repo = reconciliation_env["task_repo"]
    message_repo = reconciliation_env["message_repo"]
    reconciliation_repo = reconciliation_env["reconciliation_repo"]
    service = reconciliation_env["service"]

    task = Task(id="T-REC-1", contact_id="C-1", type=TaskType.MESSAGE, status=TaskState.RUNNING)
    task_repo.create(task)

    msg = Message(id="M-REC-1", contact_id="C-1", task_id="T-REC-1", sequence=0, body="Hello", status=MessageState.SENDING)
    message_repo.create(msg)

    record = service.enter_reconciliation(
        task_id="T-REC-1",
        message_id="M-REC-1",
        worker_id="W-1",
        reason="browser_crashed_after_send_click",
    )

    assert record is not None
    assert record.state == ReconciliationState.PENDING.value
    assert record.task_id == "T-REC-1"

    t = task_repo.get_by_id("T-REC-1")
    assert t.status == TaskState.RECONCILING

    m = message_repo.get_by_id("M-REC-1")
    assert m.status == MessageState.RECONCILIATION


def test_resolve_confirmed_sent(reconciliation_env):
    task_repo = reconciliation_env["task_repo"]
    message_repo = reconciliation_env["message_repo"]
    service = reconciliation_env["service"]

    task = Task(id="T-REC-2", contact_id="C-1", type=TaskType.MESSAGE, status=TaskState.RUNNING)
    task_repo.create(task)
    msg = Message(id="M-REC-2", contact_id="C-1", task_id="T-REC-2", sequence=0, body="Hello", status=MessageState.SENDING)
    message_repo.create(msg)

    rec = service.enter_reconciliation("T-REC-2", "timeout", message_id="M-REC-2")

    # Positive verification evidence found in thread
    res = service.resolve_reconciliation(
        reconciliation_id=rec.id,
        verification_confirmed=True,
        composer_empty=True,
        source="dm_thread_history_reader",
    )

    assert res.resolution == ReconciliationResolution.CONFIRMED_SENT

    t = task_repo.get_by_id("T-REC-2")
    assert t.status == TaskState.COMPLETED

    m = message_repo.get_by_id("M-REC-2")
    assert m.status == MessageState.SENT
    assert m.confirmed_at is not None


def test_resolve_confirmed_not_sent(reconciliation_env):
    task_repo = reconciliation_env["task_repo"]
    message_repo = reconciliation_env["message_repo"]
    service = reconciliation_env["service"]

    task = Task(id="T-REC-3", contact_id="C-1", type=TaskType.MESSAGE, status=TaskState.RUNNING)
    task_repo.create(task)
    msg = Message(id="M-REC-3", contact_id="C-1", task_id="T-REC-3", sequence=0, body="Hello", status=MessageState.SENDING)
    message_repo.create(msg)

    rec = service.enter_reconciliation("T-REC-3", "send_failed_banner", message_id="M-REC-3")

    res = service.resolve_reconciliation(
        reconciliation_id=rec.id,
        verification_confirmed=False,
        has_error_banner=True,
        source="composer_state_inspector",
    )

    assert res.resolution == ReconciliationResolution.CONFIRMED_NOT_SENT

    t = task_repo.get_by_id("T-REC-3")
    assert t.status == TaskState.RETRY_WAIT

    m = message_repo.get_by_id("M-REC-3")
    assert m.status == MessageState.FAILED


def test_resolve_retry_allowed(reconciliation_env):
    task_repo = reconciliation_env["task_repo"]
    message_repo = reconciliation_env["message_repo"]
    service = reconciliation_env["service"]

    task = Task(id="T-REC-3B", contact_id="C-1", type=TaskType.MESSAGE, status=TaskState.RUNNING)
    task_repo.create(task)
    msg = Message(id="M-REC-3B", contact_id="C-1", task_id="T-REC-3B", sequence=0, body="Hello", status=MessageState.SENDING)
    message_repo.create(msg)

    rec = service.enter_reconciliation("T-REC-3B", "navigation_interrupted", message_id="M-REC-3B")

    res = service.resolve_reconciliation(
        reconciliation_id=rec.id,
        verification_confirmed=False,
        composer_empty=False,
        source="composer_state_inspector",
    )

    assert res.resolution == ReconciliationResolution.RETRY_ALLOWED

    t = task_repo.get_by_id("T-REC-3B")
    assert t.status == TaskState.READY

    m = message_repo.get_by_id("M-REC-3B")
    assert m.status == MessageState.PENDING


def test_resolve_manual_review_escalation(reconciliation_env):
    task_repo = reconciliation_env["task_repo"]
    message_repo = reconciliation_env["message_repo"]
    manual_review_repo = reconciliation_env["manual_review_repo"]
    service = reconciliation_env["service"]

    task = Task(id="T-REC-4", contact_id="C-1", type=TaskType.MESSAGE, status=TaskState.RUNNING)
    task_repo.create(task)
    msg = Message(id="M-REC-4", contact_id="C-1", task_id="T-REC-4", sequence=0, body="Hello", status=MessageState.SENDING)
    message_repo.create(msg)

    rec = service.enter_reconciliation("T-REC-4", "unknown_instagram_dom", message_id="M-REC-4")

    # Ambiguous outcome with no verification evidence escalates to MANUAL_REVIEW
    res = service.resolve_reconciliation(
        reconciliation_id=rec.id,
        verification_confirmed=None,
        composer_empty=None,
        source="operator_or_system",
    )

    assert res.resolution == ReconciliationResolution.MANUAL_REVIEW
    t = task_repo.get_by_id("T-REC-4")
    assert t.status == TaskState.MANUAL_REVIEW

    # Review item must be created in manual_reviews table
    review = manual_review_repo.get_by_task_id("T-REC-4")
    assert review is not None
    assert review.task_id == "T-REC-4"
    assert review.status == "PENDING"
