"""Unit tests for Phase 4 manual review service, explicit resolutions, and safety."""

import pytest
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.task_repo import TaskRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.manual_review_repo import ManualReviewRepository
from backend.repositories.event_repo import EventRepository
from backend.application.manual_review_service import ManualReviewService
from backend.domain.models import Task, Message, Contact, ManualReviewItem
from backend.domain.enums import TaskState, TaskType, MessageState


@pytest.fixture
def review_env(tmp_path):
    db_path = str(tmp_path / "test_manual_review.db")
    db = DatabaseManager(db_path)
    MigrationRunner(db).apply_pending()

    task_repo = TaskRepository(db)
    msg_repo = MessageRepository(db)
    contact_repo = ContactRepository(db)
    review_repo = ManualReviewRepository(db)
    event_repo = EventRepository(db)

    service = ManualReviewService(
        manual_review_repo=review_repo,
        task_repo=task_repo,
        message_repo=msg_repo,
        event_repo=event_repo,
    )

    contact_repo.create(Contact(id="c-mr-1", name="Review Contact", instagram_url="https://instagram.com/mr1"))

    return {
        "service": service,
        "task_repo": task_repo,
        "msg_repo": msg_repo,
        "review_repo": review_repo,
    }


def test_manual_review_confirm_sent(review_env):
    service = review_env["service"]
    task_repo = review_env["task_repo"]
    msg_repo = review_env["msg_repo"]
    review_repo = review_env["review_repo"]

    task_repo.create(Task(id="t-mr-1", contact_id="c-mr-1", type=TaskType.MESSAGE, status=TaskState.MANUAL_REVIEW))
    msg_repo.create(Message(id="m-mr-1", contact_id="c-mr-1", task_id="t-mr-1", body="Test MR", status=MessageState.RECONCILIATION))
    review_repo.create(
        ManualReviewItem(id="rev-1", task_id="t-mr-1", contact_id="c-mr-1", reason="Ambiguous", current_state="SENDING")
    )

    # Operator confirms sent
    assert service.confirm_sent("rev-1", operator="alice", notes="Found message in recipient inbox") is True

    # Task should be COMPLETED, message SENT
    t = task_repo.get_by_id("t-mr-1")
    assert t.status == TaskState.COMPLETED
    m = msg_repo.get_by_id("m-mr-1")
    assert m.status == MessageState.SENT


def test_manual_review_allow_retry_resets_to_ready_without_direct_send(review_env):
    service = review_env["service"]
    task_repo = review_env["task_repo"]
    msg_repo = review_env["msg_repo"]
    review_repo = review_env["review_repo"]

    task_repo.create(Task(id="t-mr-2", contact_id="c-mr-1", type=TaskType.MESSAGE, status=TaskState.MANUAL_REVIEW))
    msg_repo.create(Message(id="m-mr-2", contact_id="c-mr-1", task_id="t-mr-2", body="Test MR Retry", status=MessageState.FAILED))
    review_repo.create(
        ManualReviewItem(id="rev-2", task_id="t-mr-2", contact_id="c-mr-1", reason="Network drop", current_state="SENDING")
    )

    # Operator allows retry
    assert service.allow_retry("rev-2", operator="bob", notes="Verified message never delivered") is True

    # Task is reset to READY (eligible for scheduler), NOT directly executed
    t = task_repo.get_by_id("t-mr-2")
    assert t.status == TaskState.READY


def test_manual_review_cancel(review_env):
    service = review_env["service"]
    task_repo = review_env["task_repo"]
    msg_repo = review_env["msg_repo"]
    review_repo = review_env["review_repo"]

    task_repo.create(Task(id="t-mr-3", contact_id="c-mr-1", type=TaskType.MESSAGE, status=TaskState.MANUAL_REVIEW))
    msg_repo.create(Message(id="m-mr-3", contact_id="c-mr-1", task_id="t-mr-3", body="Test MR Cancel", status=MessageState.FAILED))
    review_repo.create(
        ManualReviewItem(id="rev-3", task_id="t-mr-3", contact_id="c-mr-1", reason="Invalid target", current_state="READY")
    )

    assert service.cancel("rev-3", operator="carol", notes="Account is private") is True

    t = task_repo.get_by_id("t-mr-3")
    assert t.status == TaskState.CANCELLED
    m = msg_repo.get_by_id("m-mr-3")
    assert m.status == MessageState.SKIPPED
