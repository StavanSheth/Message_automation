"""Unit tests for persistent browser profiles, locking, and recovery (Section 12).

Tests:
1. Profile creation
2. Profile reuse
3. Missing profile directory recovery
4. Locked profile detection
5. Corrupted profile handling
6. Concurrent profile access rejection
"""

import os
import json
import pytest
from pathlib import Path

from backend.browser.profiles import BrowserProfileManager, BrowserProfile
from backend.browser.browser_types import BrowserType
from backend.domain.errors import ValidationError


def test_profile_creation(tmp_path):
    """1. Profile creation: creates directory and returns active profile."""
    base_dir = tmp_path / "profiles"
    mgr = BrowserProfileManager(base_directory=str(base_dir))

    profile = mgr.create_or_get_profile("account_marketing_01")
    assert profile is not None
    assert profile.profile_name == "account_marketing_01"
    assert os.path.isdir(profile.profile_path)
    assert profile.status == "ACTIVE"


def test_profile_reuse(tmp_path):
    """2. Profile reuse: returns identical path across instances and preserves files."""
    base_dir = tmp_path / "profiles"
    mgr1 = BrowserProfileManager(base_directory=str(base_dir))
    p1 = mgr1.create_or_get_profile("account_sales_01")

    # Simulate browser writing cookie/session file
    cookie_file = Path(p1.profile_path) / "Cookies.dat"
    cookie_file.write_text("dummy_session_cookie_data", encoding="utf-8")

    # Second launch: new manager discovers existing profile
    mgr2 = BrowserProfileManager(base_directory=str(base_dir))
    p2 = mgr2.create_or_get_profile("account_sales_01")

    assert p2.profile_path == p1.profile_path
    assert os.path.isfile(cookie_file)
    assert cookie_file.read_text(encoding="utf-8") == "dummy_session_cookie_data"


def test_missing_profile_directory_recovery(tmp_path):
    """3. Missing profile directory: auto-recreates base directory if missing."""
    base_dir = tmp_path / "custom_profiles"
    assert not base_dir.exists()

    mgr = BrowserProfileManager(base_directory=str(base_dir))
    assert base_dir.exists()
    p = mgr.create_or_get_profile("account_dev")
    assert os.path.isdir(p.profile_path)


def test_locked_profile_detection(tmp_path):
    """4. Locked profile: detect active lock and report status."""
    base_dir = tmp_path / "profiles"
    mgr = BrowserProfileManager(base_directory=str(base_dir))
    p = mgr.create_or_get_profile("account_locked_test")

    # Initially unlocked
    assert mgr.is_locked(p.profile_id) is False

    # Acquire lock with current PID
    mgr.acquire_lock(p.profile_id, worker_id="WKR-01", account_id="acc_01")
    assert mgr.is_locked(p.profile_id) is True

    # Release lock
    mgr.release_lock(p.profile_id, worker_id="WKR-01")
    assert mgr.is_locked(p.profile_id) is False


def test_corrupted_profile_lock_recovery(tmp_path):
    """5. Corrupted profile lock: recovers cleanly when lockfile is corrupted JSON."""
    base_dir = tmp_path / "profiles"
    mgr = BrowserProfileManager(base_directory=str(base_dir))
    p = mgr.create_or_get_profile("account_corrupted_lock")

    lock_file = Path(p.profile_path) / ".profile.lock"
    lock_file.write_text("CORRUPTED_NON_JSON_DATA", encoding="utf-8")

    # is_locked should safely handle corruption and return False
    assert mgr.is_locked(p.profile_id) is False

    # acquire_lock should clear corrupted lockfile and succeed
    acquired = mgr.acquire_lock(p.profile_id, worker_id="WKR-02")
    assert acquired is True


def test_concurrent_profile_access_rejection(tmp_path):
    """6. Concurrent profile access: second worker cannot acquire an active lock."""
    base_dir = tmp_path / "profiles"
    mgr = BrowserProfileManager(base_directory=str(base_dir))
    p = mgr.create_or_get_profile("account_exclusive")

    # Worker 1 acquires lock
    mgr.acquire_lock(p.profile_id, worker_id="WKR-01", account_id="acc_user1")

    # Worker 2 attempts acquiring same locked profile -> rejected
    with pytest.raises(ValidationError) as exc:
        mgr.acquire_lock(p.profile_id, worker_id="WKR-02", account_id="acc_user2")
    assert "Simultaneous access is forbidden" in str(exc.value)
