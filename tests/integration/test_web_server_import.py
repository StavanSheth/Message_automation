"""Integration test for web server import endpoint."""

import json
import urllib.request
from backend.bootstrap import build_production_app
from backend.web.server import DashboardServer

SAMPLE_TSV = (
    "Id No\tClient Name\tBusiness Industry\tGoogle maps Link\tWebsite Link\tInstagram id/ link\tRemarks\tFollow Up 1\n"
    "201\tLittle Beast Medlock\tRestaurant\thttps://maps.google.com\thttps://littlebeast.co.uk\thttps://www.instagram.com/littlebeastmcr\tPending\tPending\n"
)


def test_server_import_endpoint(tmp_path):
    db_file = str(tmp_path / "server_test.db")
    app = build_production_app(db_path=db_file)
    server = DashboardServer(app=app, host="127.0.0.1", port=8099)
    url = server.start(background=True)

    try:
        req = urllib.request.Request(
            f"{url}/api/source/import",
            data=json.dumps({"raw_data": SAMPLE_TSV, "message_template": "Welcome {name}!"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["success"] is True
            assert data["records_read"] == 1
            assert data["ready"] == 1

        # Verify task is created in DB
        task = app.task_repo.get_by_contact_and_type("201", "MESSAGE", 0)
        assert task is not None
        assert task.status.value == "READY"

        msg = app.message_repo.get_by_task_id(task.id)
        assert msg.body == "Welcome Little Beast Medlock!"

    finally:
        server.stop()
        app.stop()
