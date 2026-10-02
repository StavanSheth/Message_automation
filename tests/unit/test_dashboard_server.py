"""Unit tests for web dashboard server and APIs."""

import json
import urllib.request
import pytest
from backend.bootstrap import build_production_app
from backend.web.server import DashboardServer
from tests.fixtures.realistic_browser_harness import RealisticBrowserDriverHarness


@pytest.fixture
def test_app_and_server(tmp_path):
    db_file = str(tmp_path / "dash_test.db")
    app = build_production_app(db_path=db_file, driver_factory=lambda: RealisticBrowserDriverHarness())
    app.start()

    # Pick an ephemeral or high test port
    server = DashboardServer(app=app, host="127.0.0.1", port=8999)
    base_url = server.start(background=True)

    yield app, base_url

    server.stop()
    app.stop()


def test_dashboard_root_html(test_app_and_server):
    app, base_url = test_app_and_server
    req = urllib.request.Request(f"{base_url}/")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        assert "text/html" in resp.headers.get("Content-Type", "")
        body = resp.read().decode("utf-8")
        assert "Message Automation" in body
        assert "Execution Pipeline" in body


def test_dashboard_api_status(test_app_and_server):
    app, base_url = test_app_and_server
    req = urllib.request.Request(f"{base_url}/api/status")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert "system_state" in data
        assert data["system_state"] == "RUNNING"
        assert "workers" in data
        assert "tasks" in data


def test_dashboard_control_endpoints(test_app_and_server):
    app, base_url = test_app_and_server

    # POST Pause
    req_pause = urllib.request.Request(f"{base_url}/api/control/pause", data=b"", method="POST")
    with urllib.request.urlopen(req_pause) as resp:
        assert resp.status == 200
        res = json.loads(resp.read().decode("utf-8"))
        assert res.get("success") is True
        assert app.control_service.state.value == "PAUSED"

    # POST Resume
    req_resume = urllib.request.Request(f"{base_url}/api/control/resume", data=b"", method="POST")
    with urllib.request.urlopen(req_resume) as resp:
        assert resp.status == 200
        res = json.loads(resp.read().decode("utf-8"))
        assert res.get("success") is True
        assert app.control_service.state.value == "RUNNING"
