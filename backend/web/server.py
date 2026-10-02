"""HTTP Dashboard Server providing real-time operational observability and system controls."""

import json
import threading
from pathlib import Path
from typing import Optional, Dict, Any
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

from backend.bootstrap import ProductionApp
from backend.events.logger import get_logger

logger = get_logger("dashboard_server")


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Multi-threaded HTTP server so live dashboard polling does not block control actions."""
    daemon_threads = True
    allow_reuse_address = True


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """Request handler dispatching dashboard UI and JSON API endpoints."""

    def log_message(self, format, *args):
        # Suppress noisy standard request logging to keep console clean
        pass

    @property
    def app(self) -> ProductionApp:
        return self.server.app  # type: ignore

    def _send_json(self, status_code: int, data: Dict[str, Any]) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def _send_html(self, content: str) -> None:
        payload = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]

        if path in ("/", "/dashboard"):
            template_path = Path(__file__).parent / "templates" / "dashboard.html"
            if template_path.exists():
                content = template_path.read_text(encoding="utf-8")
                self._send_html(content)
            else:
                self._send_json(404, {"error": "Dashboard template not found"})
            return

        if path == "/api/status":
            try:
                base_status = self.app.status_service.get_system_status() if self.app.status_service else {}

                # Enrich with live workers
                workers = []
                if self.app.worker_manager:
                    for w in self.app.worker_manager.list_workers():
                        workers.append({
                            "worker_id": w.id,
                            "mode": w.mode.value if hasattr(w.mode, "value") else str(w.mode),
                            "status": w.status.value if hasattr(w.status, "value") else str(w.status),
                            "current_task_id": w.current_task_id,
                            "account_id": getattr(w, "account_id", None),
                            "last_heartbeat": getattr(w, "last_heartbeat", None),
                        })
                base_status["worker_list"] = workers

                # Enrich with recent tasks
                recent_tasks = []
                if self.app.task_repo:
                    try:
                        conn = self.app.task_repo.db.get_connection()
                        cur = conn.execute("SELECT id, contact_id, type, priority, status, scheduled_at FROM tasks ORDER BY priority DESC, created_at DESC LIMIT 15;")
                        for row in cur.fetchall():
                            recent_tasks.append({
                                "id": row[0],
                                "contact_id": row[1],
                                "type": row[2],
                                "priority": row[3],
                                "status": row[4],
                                "scheduled_at": row[5],
                            })
                    except Exception as e:
                        logger.debug(f"Could not fetch recent tasks for dashboard: {e}")
                base_status["recent_tasks"] = recent_tasks

                # Enrich with recent events
                recent_events = []
                if self.app.event_repo:
                    try:
                        events = self.app.event_repo.list_events(limit=25)
                        for ev in events:
                            recent_events.append({
                                "id": ev.id,
                                "timestamp": ev.timestamp,
                                "event_code": ev.event_code.value if hasattr(ev.event_code, "value") else str(ev.event_code),
                                "level": ev.level.value if hasattr(ev.level, "value") else str(ev.level),
                                "entity_type": ev.entity_type,
                                "task_id": ev.task_id,
                                "worker_id": ev.worker_id,
                            })
                    except Exception as e:
                        logger.debug(f"Could not fetch recent events for dashboard: {e}")
                base_status["recent_events"] = recent_events

                self._send_json(200, base_status)
            except Exception as e:
                logger.error(f"Error serving status API: {e}")
                self._send_json(500, {"error": str(e)})
            return

        if path == "/api/workers":
            workers = []
            if self.app.worker_manager:
                workers = [w.__dict__ for w in self.app.worker_manager.list_workers()]
            self._send_json(200, {"workers": workers})
            return

        if path == "/healthz":
            self._send_json(200, {"status": "ok", "state": self.app.control_service.state.value if self.app.control_service else "UNKNOWN"})
            return

        self._send_json(404, {"error": f"Path not found: {path}"})

    def do_POST(self) -> None:
        path = self.path.split("?")[0]

        if path == "/api/control/pause":
            res = self.app.pause("Dashboard operator requested pause")
            self._send_json(200, {"success": res, "message": "System pause command issued"})
            return

        if path == "/api/control/resume":
            res = self.app.resume("Dashboard operator requested resume")
            self._send_json(200, {"success": res, "message": "System resume command issued"})
            return

        if path == "/api/control/drain":
            res = self.app.control_service.drain("Dashboard operator requested drain") if self.app.control_service else False
            self._send_json(200, {"success": res, "message": "System drain command issued"})
            return

        if path == "/api/scheduler/tick":
            dispatched = 0
            if self.app.scheduler:
                dispatched = self.app.scheduler.tick()
            self._send_json(200, {"success": True, "dispatched": dispatched})
            return

        self._send_json(404, {"error": f"Unknown POST endpoint: {path}"})


class DashboardServer:
    """Wraps HTTP server lifecycle tied to a ProductionApp instance."""

    def __init__(self, app: ProductionApp, host: str = "127.0.0.1", port: int = 8080):
        self.app = app
        self.host = host
        self.port = port
        self.server: Optional[ThreadedHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self, background: bool = True) -> str:
        """Start the HTTP server on configured host and port. Returns the base URL."""
        self.server = ThreadedHTTPServer((self.host, self.port), DashboardRequestHandler)
        self.server.app = self.app  # type: ignore

        url = f"http://{self.host}:{self.port}"
        logger.info(f"Dashboard server listening at {url}")

        if background:
            self._thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="DashboardServerThread")
            self._thread.start()
        else:
            self.server.serve_forever()
        return url

    def stop(self) -> None:
        """Shut down the HTTP server cleanly."""
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception as e:
                logger.warning(f"Error shutting down dashboard server: {e}")
            finally:
                self.server = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
            self._thread = None
        logger.info("Dashboard server stopped")


def run_dashboard_server(app: ProductionApp, host: str = "127.0.0.1", port: int = 8080) -> DashboardServer:
    """Helper creating and starting a DashboardServer in background."""
    srv = DashboardServer(app=app, host=host, port=port)
    srv.start(background=True)
    return srv
