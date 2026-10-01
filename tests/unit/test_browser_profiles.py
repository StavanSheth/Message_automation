"""Unit tests for BrowserProfileManager isolation, discovery, and path traversal protection."""

import pytest
from pathlib import Path

from backend.browser.profiles import BrowserProfileManager
from backend.browser.browser_types import BrowserType
from backend.domain.errors import ValidationError


def test_profile_creation_and_isolation(tmp_path):
    mgr = BrowserProfileManager(base_directory=str(tmp_path))
    p1 = mgr.create_profile("worker_1", BrowserType.CHROMIUM)
    p2 = mgr.create_profile("worker_2", BrowserType.CHROMIUM)

    assert p1.profile_id != p2.profile_id
    assert p1.profile_path != p2.profile_path
    assert Path(p1.profile_path).exists()
    assert Path(p2.profile_path).exists()


def test_profile_path_traversal_protection(tmp_path):
    mgr = BrowserProfileManager(base_directory=str(tmp_path))

    with pytest.raises(ValidationError):
        mgr.create_profile("../../etc/passwd", BrowserType.CHROMIUM)

    with pytest.raises(ValidationError):
        mgr.create_profile("..\\secret", BrowserType.CHROMIUM)


def test_discover_existing_profiles(tmp_path):
    mgr = BrowserProfileManager(base_directory=str(tmp_path))
    p1 = mgr.create_profile("discovered_user", BrowserType.CHROMIUM)

    # Simulate restart by creating new manager pointing to same disk
    new_mgr = BrowserProfileManager(base_directory=str(tmp_path))
    discovered = new_mgr.discover_existing_profiles()

    assert len(discovered) >= 1
    found = [p for p in discovered if p.profile_name == "discovered_user"]
    assert len(found) == 1
    assert found[0].profile_id == p1.profile_id


def test_profile_delete(tmp_path):
    mgr = BrowserProfileManager(base_directory=str(tmp_path))
    p = mgr.create_profile("to_delete", BrowserType.CHROMIUM)
    assert mgr.get_profile(p.profile_id) is not None

    deleted = mgr.delete_profile(p.profile_id)
    assert deleted is True
    assert mgr.get_profile(p.profile_id) is None
