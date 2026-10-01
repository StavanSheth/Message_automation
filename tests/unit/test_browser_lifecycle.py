"""Unit tests for BrowserLifecycleManager registration, cleanup, and crash recovery."""

import pytest
from backend.browser.lifecycle import BrowserLifecycleManager
from backend.browser.browser_types import SessionStatus
from tests.fixtures.mock_browser import create_mock_session


def test_lifecycle_register_and_unregister():
    mgr = BrowserLifecycleManager()
    session = create_mock_session("SESS-L1")
    mgr.register_session(session)
    assert "SESS-L1" in mgr._active_sessions

    mgr.unregister_session("SESS-L1")
    assert "SESS-L1" not in mgr._active_sessions


def test_lifecycle_stop_session():
    mgr = BrowserLifecycleManager()
    session = create_mock_session("SESS-L2")
    session.start()
    mgr.register_session(session)

    mgr.stop_session("SESS-L2")
    assert session.status == SessionStatus.STOPPED
    assert "SESS-L2" not in mgr._active_sessions


def test_lifecycle_recover_session():
    mgr = BrowserLifecycleManager()
    session = create_mock_session("SESS-L3")
    session.start()
    mgr.register_session(session)

    recovered = mgr.recover_session("SESS-L3")
    assert recovered.status == SessionStatus.READY
    assert recovered.session_id == "SESS-L3"


def test_lifecycle_recover_unregistered_raises():
    mgr = BrowserLifecycleManager()
    with pytest.raises(KeyError):
        mgr.recover_session("NON-EXISTENT")


def test_lifecycle_cleanup_all_idempotent():
    mgr = BrowserLifecycleManager()
    s1 = create_mock_session("SESS-L4")
    s2 = create_mock_session("SESS-L5")
    s1.start()
    s2.start()

    mgr.register_session(s1)
    mgr.register_session(s2)

    mgr.cleanup_all()
    assert s1.status == SessionStatus.STOPPED
    assert s2.status == SessionStatus.STOPPED
    assert len(mgr._active_sessions) == 0

    # Repeated cleanup must be safe
    mgr.cleanup_all()
    assert len(mgr._active_sessions) == 0
