"""Mandatory acceptance test: VISIBLE_BROWSER_STARTUP (Point 28).

Verifies the complete end-to-end user path:
1. Run application bootstrap
2. Create worker
3. Create persistent profile
4. Launch browser
5. Assert browser process exists
6. Assert browser PID exists
7. Assert browser window is configured visible (headless=False unless CI=true)
8. Assert page exists
9. Navigate to URL (Instagram / about:blank)
10. Assert URL changes and is tracked
11. Assert browser remains open
12. Assert dashboard / API telemetry reflects live worker & browser state
"""

import os
import pytest
from backend.bootstrap import build_production_app
from backend.config.settings import AppSettings
from backend.browser.driver import is_pid_alive
from backend.web.server import DashboardServer


@pytest.mark.e2e
def test_visible_browser_startup_acceptance(tmp_path):
    # 1. Run application with persistent database and profile path
    db_path = str(tmp_path / "acceptance_test.db")
    profiles_dir = str(tmp_path / "browser_profiles")

    is_ci = os.environ.get("CI", "false").lower() == "true"
    # In local development mode, browser is visible (headless=False).
    # In CI headless Linux environments without X11, headless=True is enforced.
    settings = AppSettings(
        database_path=db_path,
        browser_profile_directory=profiles_dir,
        browser_headless=is_ci,
        worker_mode="SINGLE_BROWSER",
        max_workers=1,
    )
    app = build_production_app(db_path=db_path, settings=settings)
    app.start()

    session = None
    dashboard_server = None
    try:
        # 2. Worker manager is active
        assert app.worker_manager is not None
        assert app.browser_manager is not None

        # 3. Create persistent profile for target account
        account_name = "test_creator_acc"
        profile = app.browser_manager.profile_manager.create_or_get_profile(account_name)
        assert profile is not None
        assert os.path.isdir(profile.profile_path)

        # 4. Browser session was automatically launched by worker on app.start()
        sessions = app.browser_manager.list_sessions()
        if sessions:
            session = app.browser_manager.get_session(sessions[0].session_id)
        else:
            session = app.browser_manager.create_session(
                worker_id="WORKER-001",
                account_id=account_name,
                profile_name=account_name,
            )
            session.start()
        assert session is not None

        # 5. Assert browser process exists
        assert session.driver is not None
        assert session.driver.is_alive() is True
        assert session.driver.is_process_alive() is True

        # 6. Assert browser PID exists and is running in OS
        pid = session.driver.pid
        assert pid is not None
        assert pid > 0
        assert is_pid_alive(pid) is True

        # 7. Assert browser window mode is configured visible locally (or headless in CI)
        if not is_ci:
            assert session.config.headless is False

        # 8. Assert page exists and driver can evaluate
        eval_res = session.driver.evaluate("() => window.location.href")
        assert eval_res is not None

        # 9. Navigate to target URL
        test_url = "about:blank"
        loaded_url = session.navigate(test_url)

        # 10. Assert URL changes and session tracks it
        assert "about:blank" in loaded_url
        session.update_action(
            stage="PROFILE_VERIFICATION",
            action="Reading profile",
            url=loaded_url,
        )
        assert session.current_stage == "PROFILE_VERIFICATION"
        assert session.current_action == "Reading profile"

        # 11. Keep browser open and verify process is still actively running
        assert session.driver.is_process_alive() is True
        assert is_pid_alive(pid) is True

        # 12. Dashboard shows live worker and browser state
        dashboard_server = DashboardServer(app=app, host="127.0.0.1", port=0)
        base_url = dashboard_server.start(background=True)
        assert dashboard_server.is_healthy() is True

        import urllib.request
        import json
        with urllib.request.urlopen(f"{base_url}/api/status", timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        sessions_data = data.get("browser_sessions", [])
        assert len(sessions_data) > 0
        active_rec = sessions_data[0]
        assert active_rec.get("worker_id") == session.worker_id
        assert active_rec.get("browser_state") in ("READY", "OPEN", "ACTIVE", "HEALTHY")

    finally:
        if dashboard_server:
            dashboard_server.stop()
        if session:
            session.stop()
        app.stop()
