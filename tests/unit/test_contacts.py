"""Unit tests for Contact repository and Replied status handling."""

import pytest
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.contact_repo import ContactRepository
from backend.domain.models import Contact
from backend.domain.enums import RepliedStatus, RepliedSource


@pytest.fixture
def contact_repo(tmp_path):
    db = DatabaseManager(str(tmp_path / "contacts.db"))
    MigrationRunner(db).apply_pending()
    return ContactRepository(db)


def test_contact_create_and_retrieve(contact_repo):
    contact = Contact(
        id="C-100",
        name="Sarah Connor",
        instagram_url="https://instagram.com/sarah_c",
        username="sarah_c",
        expected_followers=1500,
        notes="Important contact",
        replied_status=RepliedStatus.UNKNOWN,
    )
    contact_repo.create(contact)

    fetched = contact_repo.get_by_id("C-100")
    assert fetched is not None
    assert fetched.name == "Sarah Connor"
    assert fetched.instagram_url == "https://instagram.com/sarah_c"
    assert fetched.username == "sarah_c"
    assert fetched.expected_followers == 1500
    assert fetched.replied_status == RepliedStatus.UNKNOWN
    assert fetched.replied_source == RepliedSource.MANUAL


def test_contact_replied_status_transitions(contact_repo):
    contact = Contact(
        id="C-101",
        name="John Doe",
        instagram_url="https://instagram.com/johndoe",
        replied_status=RepliedStatus.UNKNOWN,
    )
    contact_repo.create(contact)

    # Initial state is UNKNOWN
    assert contact_repo.get_by_id("C-101").replied_status == RepliedStatus.UNKNOWN
    assert contact_repo.get_by_id("C-101").replied_at is None

    # Transition UNKNOWN -> NO
    contact_repo.update_replied_status("C-101", RepliedStatus.NO)
    assert contact_repo.get_by_id("C-101").replied_status == RepliedStatus.NO

    # Transition NO -> YES
    contact_repo.update_replied_status("C-101", RepliedStatus.YES)
    c_yes = contact_repo.get_by_id("C-101")
    assert c_yes.replied_status == RepliedStatus.YES
    assert c_yes.replied_at is not None  # Automatically recorded timestamp
