"""Unit tests for Followup repository and cancellation rules."""

import pytest
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.followup_repo import FollowupRepository
from backend.domain.models import Contact, Followup
from backend.domain.enums import FollowupStatus


@pytest.fixture
def followup_fixture(tmp_path):
    db = DatabaseManager(str(tmp_path / "followups.db"))
    MigrationRunner(db).apply_pending()
    contact_repo = ContactRepository(db)
    fu_repo = FollowupRepository(db)

    contact = Contact(
        id="C-FU-1",
        name="Followup User",
        instagram_url="https://instagram.com/fu_user",
    )
    contact_repo.create(contact)
    return fu_repo


def test_followup_create_and_query(followup_fixture):
    fu_repo = followup_fixture
    fu1 = Followup(
        id="FU-1",
        contact_id="C-FU-1",
        sequence=1,
        message="Follow-up 1 text",
        delay_seconds=86400,
        scheduled_at="2026-01-02T00:00:00Z",
        status=FollowupStatus.SCHEDULED,
    )
    fu_repo.create(fu1)

    fu2 = Followup(
        id="FU-2",
        contact_id="C-FU-1",
        sequence=2,
        message="Follow-up 2 text",
        delay_seconds=172800,
        scheduled_at="2026-01-04T00:00:00Z",
        status=FollowupStatus.PENDING,
    )
    fu_repo.create(fu2)

    contact_fus = fu_repo.list_by_contact("C-FU-1")
    assert len(contact_fus) == 2
    assert contact_fus[0].sequence == 1
    assert contact_fus[1].sequence == 2


def test_cancel_pending_followups_for_contact(followup_fixture):
    fu_repo = followup_fixture
    fu1 = Followup(
        id="FU-CANCEL-1",
        contact_id="C-FU-1",
        sequence=1,
        message="Follow-up message",
        delay_seconds=86400,
        scheduled_at="2026-01-02T00:00:00Z",
        status=FollowupStatus.SCHEDULED,
    )
    fu_repo.create(fu1)

    # Cancel on reply
    cancelled = fu_repo.cancel_pending_for_contact("C-FU-1", cancel_reason="REPLIED")
    assert cancelled == 1

    updated = fu_repo.get_by_id("FU-CANCEL-1")
    assert updated.status == FollowupStatus.CANCELLED
    assert updated.cancel_reason == "REPLIED"
    assert updated.cancelled_at is not None
