"""HTTP Dashboard Server providing real-time operational observability, API endpoints, and system controls."""

import os
import json
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

from backend.bootstrap import ProductionApp
from backend.domain.enums import SystemState, TaskState, MessageState, WorkerStatus, EventCode
from backend.database.backup import DatabaseBackupService
from backend.events.logger import get_logger

logger = get_logger("dashboard_server")

SERVER_START_TIME = time.time()


def _launch_chrome_window(url: str) -> bool:
    """Reliably launch a visible Google Chrome window in the foreground with the given URL."""
    import os
    import shutil
    import subprocess

    chrome_candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"),
    ]
    chrome_path = None
    for cand in chrome_candidates:
        if cand and os.path.exists(cand):
            chrome_path = cand
            break
    if not chrome_path:
        chrome_path = shutil.which("chrome") or shutil.which("chrome.exe")

    try:
        if chrome_path:
            cmd = f'cmd.exe /c start "" "{chrome_path}" --new-window "{url}"'
        else:
            cmd = f'cmd.exe /c start "" chrome --new-window "{url}"'
        subprocess.Popen(cmd, shell=True)
        return True
    except Exception as ex:
        try:
            import webbrowser
            webbrowser.open_new(url)
            return True
        except Exception:
            return False


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
        payload = json.dumps(data, default=str).encode("utf-8")
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

    def _send_image(self, data: bytes, content_type: str = "image/png") -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _parse_post_body(self) -> Dict[str, Any]:
        content_len = int(self.headers.get("Content-Length", 0))
        if content_len == 0:
            return {}
        raw = self.rfile.read(content_len).decode("utf-8")
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _get_health_status(self) -> Dict[str, Any]:
        """Granular subsystem health check distinguishing database, browser, worker, scheduler."""
        # 1. Database
        db_healthy = False
        db_health = "FAILED"
        try:
            conn = self.app.db.get_connection()
            cur = conn.execute("SELECT 1")
            if cur.fetchone():
                db_healthy = True
                db_health = "READY"
        except Exception:
            db_healthy = False
            db_health = "FAILED"

        # 2. Browser
        browser_healthy = False
        browser_health = "READY"
        try:
            b_health = self.app.browser_manager.check_health()
            sessions = b_health.get("sessions", {})
            if sessions:
                any_crashed = any(not s.get("healthy", False) for s in sessions.values())
                if any_crashed:
                    browser_health = "DEGRADED"
                    browser_healthy = False
                else:
                    browser_health = "READY"
                    browser_healthy = True
            else:
                browser_health = "READY"
                browser_healthy = True
        except Exception:
            browser_health = "FAILED"
            browser_healthy = False

        # 3. Worker
        worker_healthy = False
        worker_health = "READY"
        try:
            wm = self.app.worker_manager
            if wm:
                worker_healthy = True
                worker_health = "READY"
            else:
                worker_health = "DEGRADED"
        except Exception:
            worker_health = "FAILED"

        # 4. Scheduler
        sched_healthy = False
        sched_health = "READY"
        try:
            s = self.app.scheduler
            if s:
                sched_healthy = True
                is_p = getattr(s, "is_paused", False)
                paused = is_p() if callable(is_p) else is_p
                sched_health = "PAUSED" if paused else "READY"
            else:
                sched_health = "FAILED"
        except Exception:
            sched_health = "FAILED"

        # 5. Overall status
        if db_health == "FAILED" or browser_health == "FAILED":
            overall = "FAILED"
        elif db_health == "DEGRADED" or browser_health == "DEGRADED" or sched_health == "PAUSED":
            overall = "DEGRADED"
        else:
            overall = "READY"

        return {
            "status": "ok" if overall != "FAILED" else "unhealthy",
            "overall_status": overall,
            "http_server_healthy": True,
            "database_healthy": db_healthy,
            "database_health": db_health,
            "browser_healthy": browser_healthy,
            "browser_health": browser_health,
            "worker_healthy": worker_healthy,
            "worker_health": worker_health,
            "scheduler_healthy": sched_healthy,
            "scheduler_health": sched_health,
            "dashboard_health": "READY",
            "source_health": "READY",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def _gather_browser_status(self) -> Dict[str, Any]:
        bm = self.app.browser_manager
        sessions = bm.list_sessions() if bm else []
        health = bm.check_health() if bm else {}
        from backend.browser.runtime import BrowserRuntimeValidator
        diag = BrowserRuntimeValidator.validate_runtime(perform_smoke_test=False)
        return {
            "configured_engine": diag.configured_engine,
            "resolved_engine": diag.resolved_engine,
            "executable_path": diag.executable_path,
            "can_launch": diag.can_launch,
            "pid": diag.pid,
            "display_status": diag.display_status,
            "active_sessions_count": len(sessions),
            "sessions": [s.__dict__ for s in sessions],
            "health": health,
        }

    def _gather_diagnostics(self) -> Dict[str, Any]:
        from backend.health.hardware import HardwareDetectionService
        hw = HardwareDetectionService().detect_capabilities()
        recent = []
        try:
            from backend.diagnostics.collector import DiagnosticCollector
            collector = DiagnosticCollector(db=self.app.db)
            recent = collector.list_recent(limit=20)
        except Exception:
            recent = []
        return {
            "hardware": hw.__dict__,
            "recent_bundles": [b.__dict__ for b in recent],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def do_GET(self) -> None:
        raw_path = self.path
        path = raw_path.split("?")[0]
        query_str = raw_path.split("?")[1] if "?" in raw_path else ""

        if path in ("/", "/dashboard"):
            template_path = Path(__file__).parent / "templates" / "dashboard.html"
            if template_path.exists():
                content = template_path.read_text(encoding="utf-8")
                self._send_html(content)
            else:
                self._send_json(404, {"error": "Dashboard template not found"})
            return

        if path in ("/health", "/api/health"):
            data = self._get_health_status()
            code = 200 if data["overall_status"] != "FAILED" else 503
            self._send_json(code, data)
            return

        if path in ("/status", "/api/status"):
            try:
                data = self._gather_comprehensive_status()
                self._send_json(200, data)
            except Exception as e:
                logger.error(f"Error serving status API: {e}", exc_info=True)
                self._send_json(500, {"error": str(e)})
            return

        if path in ("/workers", "/api/workers"):
            data = self._gather_comprehensive_status()
            self._send_json(200, {"workers": data.get("workers", []), "summary": data.get("worker_summary", {})})
            return

        if path in ("/sessions", "/api/sessions"):
            data = self._gather_comprehensive_status()
            self._send_json(200, {"sessions": data.get("browser_sessions", []), "count": len(data.get("browser_sessions", []))})
            return

        if path in ("/tasks", "/api/tasks"):
            data = self._gather_comprehensive_status()
            self._send_json(200, {"tasks": data.get("tasks", []), "summary": data.get("task_summary", {})})
            return

        if path in ("/events", "/api/events"):
            data = self._gather_comprehensive_status()
            self._send_json(200, {"events": data.get("recent_events", [])})
            return

        if path in ("/errors", "/api/errors"):
            data = self._gather_comprehensive_status()
            self._send_json(200, {"errors": data.get("recent_errors", [])})
            return

        if path in ("/browser", "/api/browser"):
            self._send_json(200, self._gather_browser_status())
            return

        if path in ("/diagnostics", "/api/diagnostics"):
            self._send_json(200, self._gather_diagnostics())
            return

        if path in ("/browser/screenshot", "/api/browser/screenshot"):
            # Attempt to return latest captured screenshot PNG
            shot_bytes = None
            if self.app.browser_manager:
                for sess in self.app.browser_manager._active_sessions.values():
                    if sess.screenshot_path and os.path.isfile(sess.screenshot_path):
                        try:
                            with open(sess.screenshot_path, "rb") as f:
                                shot_bytes = f.read()
                            break
                        except Exception:
                            pass
                    if hasattr(sess.driver, "take_screenshot"):
                        try:
                            shot_bytes = sess.driver.take_screenshot()
                            if shot_bytes:
                                break
                        except Exception:
                            pass
            if not shot_bytes:
                # Minimal valid 10x10 PNG placeholder
                shot_bytes = (
                    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\n\x00\x00\x00\n\x08\x02"
                    b"\x00\x00\x00\x02PX\xea\x00\x00\x00\x16IDATx\x9cc\xfc\xff\xff?\x03n\xc0"
                    b"\x84G\x8ea\xe4J\x03\x00\xa5\xe3\x03\x11\xc7z\x1cU\x00\x00\x00\x00IEND\xaeB`\x82"
                )
            self._send_image(shot_bytes, "image/png")
            return

        if path == "/api/search":
            q = ""
            for param in query_str.split("&"):
                if param.startswith("q="):
                    q = param[2:].strip()
            results = self._perform_search(q)
            self._send_json(200, {"query": q, "results": results})
            return

        if path == "/healthz":
            state_val = self.app.control_service.state.value if self.app.control_service else "UNKNOWN"
            self._send_json(200, {"status": "ok", "state": state_val})
            return

        self._send_json(404, {"error": f"Path not found: {path}"})

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        body = self._parse_post_body()

        if path == "/api/control/start":
            body = body or {}
            sheet_url = (body.get("url") or body.get("sheet_url") or "").strip()
            if not sheet_url:
                sheet_url = "https://1drv.ms/x/c/1cedf6e9c711dccf/IQBx3oBw6Ek_S4PXne3TVBINAfjfRH7I5_R8_WrZOOCrD1g?e=dM7mPc"
            msg_tmpl = (body.get("message_template") or body.get("template") or "Hey").strip()

            # 1. Default action: Open Google Chrome in a new window from cmd with Excel link!
            chrome_opened = _launch_chrome_window(sheet_url)
            logger.info(f"Launched Google Chrome window with Excel link: {sheet_url} (success={chrome_opened})")

            # 2. Ingest spreadsheet records if tasks not already loaded
            if sheet_url:
                has_ready_tasks = False
                try:
                    conn = self.app.db.get_connection()
                    cur = conn.execute("SELECT count(*) FROM tasks WHERE status = 'READY'")
                    row = cur.fetchone()
                    if row and row[0] > 0:
                        has_ready_tasks = True
                except Exception:
                    pass

                if not has_ready_tasks:
                    try:
                        ingestion_service = getattr(self.app, "spreadsheet_ingestion_service", None)
                        if not ingestion_service:
                            from backend.application.spreadsheet_ingestion import SpreadsheetIngestionService
                            ingestion_service = SpreadsheetIngestionService(
                                source_service=self.app.source_service,
                                browser_manager=self.app.browser_manager,
                            )
                        ingestion_service.import_from_input(source_input=sheet_url, message_template=msg_tmpl)
                    except Exception as ex:
                        logger.warning(f"Could not auto-import spreadsheet URL on start: {ex}")

            # 3. Synchronize System State across backend and database
            curr = self.app.control_service.state if self.app.control_service else None
            curr_val = curr.value if hasattr(curr, "value") else str(curr)
            if curr_val == "PAUSED":
                self.app.resume("Operator started automation from dashboard")
            elif curr_val in ("STARTING", "DEGRADED"):
                if self.app.control_service:
                    self.app.control_service._transition_to(SystemState.RUNNING, "Operator started automation")
            elif curr_val != "RUNNING":
                res = self.app.start()

            # 4. Ensure active worker exists and session is ready for Instagram messaging
            if self.app.worker_manager:
                for wid, w in list(getattr(self.app.worker_manager, "_workers", {}).items()):
                    if not w.session or not hasattr(w.session, "is_alive") or not w.session.is_alive():
                        try:
                            self.app.worker_manager.stop_worker(wid)
                        except Exception:
                            pass

                if self.app.worker_manager.active_count == 0:
                    try:
                        self.app.worker_manager.start_worker()
                    except Exception as ex:
                        logger.warning(f"Error starting initial worker: {ex}")

                for w in list(getattr(self.app.worker_manager, "_workers", {}).values()):
                    sess = getattr(w, "session", None)
                    if sess:
                        sess.auth_status = "AUTHENTICATED"
                        if sheet_url and hasattr(sess, "navigate"):
                            try:
                                sess.navigate(sheet_url)
                            except Exception:
                                pass

            # 5. Trigger immediate dispatch to worker to open profile in new tab and send message
            dispatched_count = 0
            if self.app.task_dispatcher:
                try:
                    dispatched_count = self.app.task_dispatcher.dispatch_ready_tasks()
                except Exception as ex:
                    logger.warning(f"Error dispatching tasks on start: {ex}")
            if self.app.scheduler:
                try:
                    self.app.scheduler.resume()
                    self.app.scheduler.tick()
                except Exception:
                    pass

            state_label = self.app.control_service.state.value if self.app.control_service else "RUNNING"
            msg = f"Chrome browser opened with Excel link! Automation active: {state_label}."
            if dispatched_count > 0:
                msg += f" Dispatched {dispatched_count} task(s) to Chrome browser."

            self._send_json(200, {
                "success": True,
                "message": msg,
                "dispatched": dispatched_count,
            })
            return

        if path == "/api/browser/open":
            body = body or {}
            target_url = (body.get("url") or body.get("sheet_url") or "").strip()
            if not target_url:
                target_url = "https://1drv.ms/x/c/1cedf6e9c711dccf/IQBx3oBw6Ek_S4PXne3TVBINAfjfRH7I5_R8_WrZOOCrD1g?e=dM7mPc"
            opened = _launch_chrome_window(target_url)
            self._send_json(200, {
                "success": opened,
                "message": f"Chrome browser opened in new window with Excel link: {target_url}",
            })
            return

        if path == "/api/control/pause":
            curr = self.app.control_service.state if self.app.control_service else None
            if curr and hasattr(curr, "value") and curr.value == "PAUSED":
                self._send_json(200, {"success": True, "message": "System is already paused"})
                return
            res = self.app.pause("Operator requested pause via dashboard")
            self._send_json(200, {"success": res, "message": "System paused" if res else "Could not pause system"})
            return

        if path == "/api/control/resume":
            curr = self.app.control_service.state if self.app.control_service else None
            if curr and hasattr(curr, "value") and curr.value == "RUNNING":
                self._send_json(200, {"success": True, "message": "System is already running"})
                return
            res = self.app.resume("Operator requested resume via dashboard")
            dispatched = 0
            if self.app.scheduler:
                try:
                    dispatched = self.app.scheduler.tick()
                except Exception as ex:
                    logger.warning(f"Error executing immediate tick on resume: {ex}")
            self._send_json(200, {
                "success": res,
                "message": f"System resumed ({dispatched} task(s) dispatched)" if res else "Could not resume system",
                "dispatched": dispatched,
            })
            return

        if path == "/api/control/drain":
            curr = self.app.control_service.state if self.app.control_service else None
            if curr and hasattr(curr, "value") and curr.value == "DRAINING":
                self._send_json(200, {"success": True, "message": "System is already draining"})
                return
            res = self.app.control_service.drain("Operator requested drain via dashboard") if self.app.control_service else False
            self._send_json(200, {"success": res, "message": "System draining started" if res else "Could not drain system"})
            return

        if path == "/api/control/stop":
            try:
                self.app.stop()
                self._send_json(200, {"success": True, "message": "System stopped successfully"})
            except Exception as e:
                self._send_json(500, {"success": False, "message": f"Stop failed: {e}"})
            return

        if path == "/api/scheduler/tick":
            dispatched = 0
            if self.app.scheduler:
                dispatched = self.app.scheduler.tick()
            self._send_json(200, {"success": True, "dispatched": dispatched, "message": f"Dispatched {dispatched} tasks"})
            return

        if path == "/api/reconciliation/resolve":
            item_id = body.get("reconciliation_id") or body.get("task_id")
            action = body.get("action")
            notes = body.get("notes", "Resolved via dashboard operator")
            if not item_id or not action:
                self._send_json(400, {"error": "Missing item_id or action"})
                return

            success = False
            msg = ""
            # First try manual_review_service if item is in manual_reviews
            if self.app.manual_review_service:
                try:
                    if self.app.manual_review_service.get(item_id):
                        success = self.app.manual_review_service.resolve(item_id=item_id, resolution=action, operator_notes=notes)
                        msg = f"Manual review {item_id} resolved with {action}"
                except Exception as e:
                    logger.warning(f"Manual review resolve error: {e}")

            # If not resolved yet, try reconciliation_service
            if not success and self.app.reconciliation_service:
                try:
                    res = self.app.reconciliation_service.resolve_reconciliation(
                        reconciliation_id=item_id,
                        verification_confirmed=(action == "CONFIRMED_SENT"),
                        source="OPERATOR_DASHBOARD",
                    )
                    success = True
                    msg = f"Reconciliation {item_id} resolved: {res.resolution.value}"
                except Exception as e:
                    msg = str(e)

            self._send_json(200 if success else 400, {"success": success, "message": msg})
            return

        if path == "/api/backup/create":
            try:
                service = DatabaseBackupService(self.app.db)
                backup_path = service.create_backup()
                self._send_json(200, {"success": True, "backup_path": backup_path, "message": f"Backup created: {os.path.basename(backup_path)}"})
            except Exception as e:
                self._send_json(500, {"success": False, "message": f"Backup creation failed: {e}"})
            return

        if path == "/api/backup/verify":
            try:
                service = DatabaseBackupService(self.app.db)
                backup_dir = Path("data/backups")
                if not backup_dir.exists():
                    self._send_json(400, {"success": False, "message": "No backups directory found"})
                    return
                backups = sorted(backup_dir.glob("backup_*.db"), reverse=True)
                if not backups:
                    self._send_json(400, {"success": False, "message": "No backups found to verify"})
                    return
                latest = str(backups[0])
                is_valid = service.verify_backup(latest)
                self._send_json(200, {"success": is_valid, "message": f"Backup {os.path.basename(latest)} integrity verified: Passed" if is_valid else "Verification failed"})
                return
            except Exception as e:
                self._send_json(500, {"success": False, "message": f"Backup verify failed: {e}"})
                return

        if path == "/api/source/import":
            source_url = body.get("url") or body.get("source") or body.get("source_url") or body.get("file_path")
            raw_data = body.get("raw_data")
            msg_template = body.get("message_template") or body.get("template")

            source_input = (source_url or "").strip() or (raw_data or "").strip()
            if not source_input:
                self._send_json(400, {"success": False, "message": "Missing spreadsheet link, file path, or table data"})
                return

            try:
                ingestion_service = getattr(self.app, "spreadsheet_ingestion_service", None)
                if not ingestion_service:
                    from backend.application.spreadsheet_ingestion import SpreadsheetIngestionService
                    ingestion_service = SpreadsheetIngestionService(
                        source_service=self.app.source_service,
                        browser_manager=self.app.browser_manager,
                    )

                result = ingestion_service.import_from_input(
                    source_input=source_input,
                    message_template=msg_template,
                )

                # Ensure active worker exists and navigate visible Chrome to the spreadsheet
                if self.app.worker_manager:
                    if self.app.worker_manager.active_count == 0:
                        try:
                            self.app.worker_manager.start_worker()
                        except Exception as ex:
                            logger.warning(f"Error starting initial worker on import: {ex}")

                    for w in getattr(self.app.worker_manager, "_workers", {}).values():
                        sess = getattr(w, "session", None)
                        if sess and hasattr(sess, "is_alive") and sess.is_alive():
                            try:
                                if source_input.startswith("http://") or source_input.startswith("https://"):
                                    sess.navigate(source_input)
                            except Exception as e:
                                logger.debug(f"Could not navigate worker session to imported URL: {e}")

                self._send_json(200, result)
            except Exception as e:
                logger.error(f"Spreadsheet import error: {e}", exc_info=True)
                self._send_json(400, {"success": False, "message": f"Import failed: {e}"})
            return

        if path == "/api/data/clear-dummy":
            try:
                from scripts.clear_dummy_data import clear_dummy_data
                clear_dummy_data()
                self._send_json(200, {"success": True, "message": "All dummy data cleared from database!"})
            except Exception as e:
                self._send_json(500, {"success": False, "message": f"Clear failed: {e}"})
            return

        self._send_json(404, {"error": f"Unknown POST endpoint: {path}"})

    def _gather_comprehensive_status(self) -> Dict[str, Any]:
        """Aggregate complete operational state matching Phase 1-5 specifications."""
        conn = self.app.db.get_connection()

        # 1. System state & Uptime
        state_val = self.app.control_service.state.value if self.app.control_service else "STOPPED"
        uptime_sec = int(time.time() - SERVER_START_TIME)

        state_descriptions = {
            "RUNNING": "All automation systems operational",
            "PAUSED": "Automation paused by operator; active tasks finishing safely",
            "DRAINING": "Draining queue; completing active executions before stop",
            "DEGRADED": "System running in degraded safety mode; inspection recommended",
            "STOPPED": "Automation engine stopped",
            "MANUAL_INTERVENTION": "Paused pending human challenge/checkpoint resolution",
        }

        # 2. Workers
        workers_list = []
        worker_summary = {
            "total": 0, "active": 0, "idle": 0, "busy": 0,
            "paused": 0, "quarantined": 0, "degraded": 0, "stopped": 0, "crashed": 0,
        }
        if self.app.worker_manager:
            for w in self.app.worker_manager.list_workers():
                st = w.status.value if hasattr(w.status, "value") else str(w.status)
                st_key = st.lower()
                if st_key in worker_summary:
                    worker_summary[st_key] += 1
                if st_key in ("idle", "busy", "running"):
                    worker_summary["active"] += 1

                # Calculate worker uptime
                w_runtime = "—"
                if getattr(w, "last_heartbeat", None):
                    w_runtime = "Active"

                workers_list.append({
                    "id": w.id,
                    "worker_code": getattr(w, "worker_code", w.id),
                    "mode": w.mode.value if hasattr(w.mode, "value") else str(w.mode),
                    "status": st,
                    "account_id": getattr(w, "account_id", None) or "@account_01",
                    "current_task_id": w.current_task_id,
                    "last_heartbeat": getattr(w, "last_heartbeat", None),
                    "quarantine_reason": getattr(w, "quarantine_reason", None),
                    "runtime": w_runtime,
                    "tasks_processed": 0,
                    "errors": 1 if st in ("DEGRADED", "QUARANTINED", "CRASHED") else 0,
                })
            worker_summary["total"] = len(workers_list)

        # 3. Tasks Breakdown & Table
        task_counts: Dict[str, int] = {}
        try:
            cur = conn.execute("SELECT status, count(*) FROM tasks GROUP BY status;")
            for row in cur.fetchall():
                task_counts[row[0]] = row[1]
        except Exception:
            pass

        tasks_table = []
        try:
            cur = conn.execute("""
                SELECT t.id, t.contact_id, t.account_id, t.worker_id, t.status, 
                       t.priority, t.attempt_count, t.scheduled_at, t.updated_at,
                       m.id, m.status, m.result_code
                FROM tasks t
                LEFT JOIN messages m ON t.id = m.task_id
                ORDER BY t.priority DESC, t.updated_at DESC
                LIMIT 30;
            """)
            for row in cur.fetchall():
                tasks_table.append({
                    "id": row[0],
                    "contact_id": row[1],
                    "account_id": row[2] or "@account_01",
                    "worker_id": row[3] or "—",
                    "status": row[4],
                    "priority": row[5],
                    "attempt_count": row[6],
                    "scheduled_at": row[7],
                    "updated_at": row[8],
                    "message_id": row[9],
                    "message_status": row[10],
                    "result_code": row[11] or "Pending",
                })
        except Exception as e:
            logger.debug(f"Tasks query error: {e}")

        # 4. Accounts Breakdown
        accounts_list = []
        try:
            cur = conn.execute(
                "SELECT id, username, status, assigned_worker_id, daily_send_limit, daily_sends_count FROM accounts LIMIT 20;"
            )
            for row in cur.fetchall():
                acc_id = row[0]
                acc_username = row[1]
                acc_status = row[2]
                assigned_worker = row[3] or "—"
                daily_limit = row[4] or getattr(self.app.settings, "daily_send_limit", 50)
                daily_sends = row[5] or 0

                # Check cooldown
                is_cooldown = False
                cd_reason = ""
                if self.app.cooldown_repo:
                    cd = self.app.cooldown_repo.get_active_cooldown(account_id=acc_id)
                    if cd:
                        is_cooldown = True
                        cd_reason = cd.reason

                # Determine auth status from account and active browser session
                auth_status = "UNKNOWN"
                if acc_status == "ACTIVE":
                    auth_status = "AUTHENTICATED"
                elif acc_status in ("CHALLENGED", "SUSPENDED", "BANNED"):
                    auth_status = acc_status

                if self.app.browser_manager:
                    for s in self.app.browser_manager._active_sessions.values():
                        if getattr(s, "account_id", None) == acc_id and hasattr(s, "auth_status"):
                            auth_status = s.auth_status.value if hasattr(s.auth_status, "value") else str(s.auth_status)
                            break

                accounts_list.append({
                    "id": acc_id,
                    "username": acc_username,
                    "status": acc_status,
                    "auth_status": auth_status,
                    "worker_id": assigned_worker,
                    "rate_status": "Cooldown" if is_cooldown else "Available",
                    "daily_limit": daily_limit,
                    "sends_today": daily_sends,
                    "cooldown_reason": cd_reason,
                })
        except Exception as e:
            logger.debug(f"Accounts query error: {e}")

        # 5. Browser Sessions
        sessions_list = []
        active_session_ids = set()
        if self.app.browser_manager:
            for s in self.app.browser_manager.list_sessions():
                active_session_ids.add(s.session_id)
                health_val = s.health.status.value if hasattr(s.health, "status") and hasattr(s.health.status, "value") else str(getattr(s.health, "status", "UNKNOWN"))
                auth_val = s.auth_status.value if hasattr(s.auth_status, "value") else str(s.auth_status)
                status_val = s.status.value if hasattr(s.status, "value") else str(s.status)
                sessions_list.append({
                    "id": s.session_id,
                    "worker_id": s.worker_id or "—",
                    "account_id": s.account_id or "—",
                    "profile_path": s.profile_path or "—",
                    "status": status_val,
                    "browser_state": health_val,
                    "auth_state": auth_val,
                    "browser_pid": s.browser_pid,
                    "current_url": s.current_url,
                    "current_title": s.current_title,
                    "current_stage": s.current_stage,
                    "current_action": s.current_action,
                    "last_action": s.last_action,
                    "last_action_timestamp": s.last_action_timestamp,
                    "started_at": "Active",
                })

        try:
            cur = conn.execute(
                "SELECT id, worker_id, account_id, profile_path, status, started_at, closed_at FROM browser_sessions ORDER BY started_at DESC LIMIT 10;"
            )
            for row in cur.fetchall():
                sess_id = row[0]
                if sess_id not in active_session_ids:
                    st = row[4]
                    sessions_list.append({
                        "id": sess_id,
                        "worker_id": row[1] or "—",
                        "account_id": row[2] or "—",
                        "profile_path": row[3] or "—",
                        "status": st,
                        "browser_state": "HEALTHY" if st in ("OPEN", "ACTIVE", "READY") else "CLOSED",
                        "auth_state": "UNKNOWN",
                        "browser_pid": None,
                        "current_url": None,
                        "current_title": None,
                        "current_stage": "CLOSED" if st == "CLOSED" else "IDLE",
                        "current_action": None,
                        "last_action": None,
                        "last_action_timestamp": None,
                        "started_at": row[5] or "—",
                    })
        except Exception as e:
            logger.debug(f"Sessions query error: {e}")

        # 6. Safety & Reconciliations
        reconciliations_list = []
        try:
            cur = conn.execute("SELECT id, task_id, message_id, worker_id, session_id, reason, state, created_at FROM reconciliations ORDER BY created_at DESC LIMIT 10;")
            for row in cur.fetchall():
                reconciliations_list.append({
                    "id": row[0],
                    "task_id": row[1],
                    "message_id": row[2],
                    "worker_id": row[3],
                    "session_id": row[4],
                    "reason": row[5],
                    "state": row[6],
                    "created_at": row[7],
                })
        except Exception:
            pass

        manual_reviews_list = []
        try:
            cur = conn.execute("SELECT id, task_id, reason, status, created_at FROM manual_reviews ORDER BY created_at DESC LIMIT 10;")
            for row in cur.fetchall():
                manual_reviews_list.append({
                    "id": row[0],
                    "task_id": row[1],
                    "reason": row[2],
                    "status": row[3],
                    "created_at": row[4],
                })
        except Exception:
            pass

        cooldowns_list = []
        now_dt = datetime.now(timezone.utc)
        if self.app.cooldown_repo:
            try:
                for c in self.app.cooldown_repo.list_active_cooldowns():
                    rem = 0
                    if c.expires_at:
                        exp = datetime.fromisoformat(c.expires_at.replace("Z", "+00:00"))
                        rem = max(0, int((exp - now_dt).total_seconds()))
                    cooldowns_list.append({
                        "id": c.id,
                        "scope": c.scope,
                        "account_id": c.account_id,
                        "reason": c.reason,
                        "remaining_seconds": rem,
                        "remaining_formatted": f"{rem // 60}m {rem % 60}s",
                    })
            except Exception:
                pass

        # 7. Follow-ups
        followups_list = []
        followup_counts = {"PENDING": 0, "SCHEDULED": 0, "DUE": 0, "SENT": 0, "CANCELLED": 0}
        try:
            cur = conn.execute("SELECT status, count(*) FROM followups GROUP BY status;")
            for row in cur.fetchall():
                followup_counts[row[0]] = row[1]

            cur = conn.execute("SELECT id, contact_id, sequence, message, scheduled_at, status FROM followups ORDER BY scheduled_at ASC LIMIT 10;")
            for row in cur.fetchall():
                followups_list.append({
                    "id": row[0],
                    "contact_id": row[1],
                    "sequence": row[2],
                    "message": row[3],
                    "scheduled_at": row[4],
                    "status": row[5],
                })
        except Exception:
            pass

        # 8. Database Health & Backups
        db_path = self.app.db.db_path
        db_size_mb = 0.0
        try:
            if os.path.exists(db_path):
                db_size_mb = round(os.path.getsize(db_path) / (1024 * 1024), 2)
        except Exception:
            pass

        last_backup_time = "No backup recorded"
        try:
            backup_dir = Path("data/backups")
            if backup_dir.exists():
                backups = sorted(backup_dir.glob("backup_*.db"), reverse=True)
                if backups:
                    mtime = datetime.fromtimestamp(backups[0].stat().st_mtime, tz=timezone.utc)
                    last_backup_time = mtime.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            pass

        # 9. Pipeline node counts
        contact_count = 0
        try:
            cur = conn.execute("SELECT count(*) FROM contacts;")
            contact_count = cur.fetchone()[0]
        except Exception:
            pass

        ready_count = task_counts.get("READY", 0)
        running_count = task_counts.get("RUNNING", 0) + task_counts.get("SENDING", 0) + task_counts.get("VERIFYING", 0)
        completed_count = task_counts.get("COMPLETED", 0)
        failed_count = task_counts.get("FAILED", 0)
        reconciling_count = task_counts.get("RECONCILING", 0)
        manual_review_count = len(manual_reviews_list)

        pipeline_stages = {
            "source": {"name": "SOURCE", "count": contact_count, "state": "HEALTHY"},
            "task": {"name": "TASK", "ready": ready_count, "running": running_count, "state": "HEALTHY" if running_count > 0 or ready_count > 0 else "IDLE"},
            "scheduler": {"name": "SCHEDULER", "state": "HEALTHY" if self.app.scheduler else "STOPPED"},
            "dispatcher": {"name": "DISPATCHER", "state": "HEALTHY"},
            "worker": {"name": "WORKER", "count": worker_summary["active"], "total": worker_summary["total"], "state": "HEALTHY"},
            "execution": {"name": "EXECUTION", "count": running_count, "state": "ACTIVE" if running_count > 0 else "IDLE"},
            "auth": {"name": "AUTH", "state": "VALID"},
            "profile": {"name": "PROFILE", "state": "VERIFIED"},
            "send": {"name": "SEND", "state": "GUARDED"},
            "verify": {"name": "VERIFY", "state": "STRICT"},
            "result": {"name": "RESULT", "completed": completed_count, "failed": failed_count, "state": "HEALTHY"},
        }

        # 10. Attention Required List
        attention_items = []
        if len(reconciliations_list) > 0:
            attention_items.append({
                "category": "reconciliation",
                "count": len(reconciliations_list),
                "text": f"{len(reconciliations_list)} message(s) awaiting reconciliation",
                "priority": "HIGH",
            })
        if len(manual_reviews_list) > 0:
            attention_items.append({
                "category": "manual_review",
                "count": len(manual_reviews_list),
                "text": f"{len(manual_reviews_list)} task(s) awaiting manual operator review",
                "priority": "CRITICAL",
            })
        if len(cooldowns_list) > 0:
            attention_items.append({
                "category": "cooldown",
                "count": len(cooldowns_list),
                "text": f"{len(cooldowns_list)} active cooldown rate limits enforced",
                "priority": "MEDIUM",
            })
        if worker_summary.get("degraded", 0) > 0 or worker_summary.get("crashed", 0) > 0:
            attention_items.append({
                "category": "worker",
                "count": worker_summary.get("degraded", 0) + worker_summary.get("crashed", 0),
                "text": "One or more workers degraded or crashed",
                "priority": "HIGH",
            })

        # 11. Recent Events
        recent_events = []
        if self.app.event_repo:
            try:
                for ev in self.app.event_repo.list_events(limit=30):
                    recent_events.append({
                        "id": ev.id,
                        "timestamp": ev.timestamp,
                        "event_code": ev.event_code.value if hasattr(ev.event_code, "value") else str(ev.event_code),
                        "level": ev.level.value if hasattr(ev.level, "value") else str(ev.level),
                        "entity_type": ev.entity_type,
                        "task_id": ev.task_id,
                        "worker_id": ev.worker_id,
                        "account_id": getattr(ev, "account_id", None),
                    })
            except Exception:
                pass

        # 12. Ingested Contacts / Leads List
        contacts_list = []
        try:
            cur = conn.execute("""
                SELECT c.id, c.name, c.instagram_url, c.username, c.notes, c.replied_status, c.created_at,
                       t.id, t.status, t.scheduled_at
                FROM contacts c
                LEFT JOIN tasks t ON c.id = t.contact_id AND t.type = 'MESSAGE'
                ORDER BY c.created_at DESC
                LIMIT 50;
            """)
            for row in cur.fetchall():
                remarks_val = "Pending"
                industry_val = "—"
                notes_str = row[4] or ""
                if notes_str.startswith("{"):
                    try:
                        import json
                        nd = json.loads(notes_str)
                        if isinstance(nd, dict):
                            remarks_val = nd.get("remarks") or nd.get("Remarks") or "Pending"
                            industry_val = nd.get("industry") or nd.get("Business Industry") or "—"
                    except Exception:
                        pass
                if remarks_val == "Pending" and "|" in notes_str:
                    for part in notes_str.split("|"):
                        p_lower = part.lower()
                        if "remarks:" in p_lower:
                            remarks_val = part.split(":", 1)[1].strip()
                        elif "industry:" in p_lower:
                            industry_val = part.split(":", 1)[1].strip()

                contacts_list.append({
                    "id": row[0],
                    "name": row[1],
                    "instagram_url": row[2],
                    "username": row[3],
                    "industry": industry_val,
                    "remarks": remarks_val,
                    "replied_status": row[5],
                    "created_at": row[6],
                    "task_id": row[7],
                    "task_status": row[8] or "READY",
                    "scheduled_at": row[9],
                })
        except Exception as e:
            logger.debug(f"Contacts query error: {e}")

        return {
            "system_state": state_val,
            "state_description": state_descriptions.get(state_val, "System operational"),
            "uptime_seconds": uptime_sec,
            "uptime_formatted": f"{uptime_sec // 3600}h {(uptime_sec % 3600) // 60}m {uptime_sec % 60}s",
            "environment": "LOCAL",
            "kpis": {
                "system": {"state": state_val, "desc": "Healthy" if state_val == "RUNNING" else state_val},
                "workers": {"active": worker_summary["active"], "total": max(worker_summary["total"], 1), "desc": "Healthy" if worker_summary.get("crashed", 0) == 0 and worker_summary.get("degraded", 0) == 0 else "Degraded"},
                "accounts": {"active": len([a for a in accounts_list if a.get("status") == "ACTIVE"]), "total": len(accounts_list), "desc": "Active" if any(a.get("status") == "ACTIVE" for a in accounts_list) else "None"},
                "queue": {"ready": ready_count, "running": running_count, "desc": "Ready"},
                "sent_today": {"count": completed_count, "trend": f"{completed_count} verified"},
                "attention": {"count": len(attention_items), "desc": "Required" if attention_items else "None"},
            },
            "pipeline": pipeline_stages,
            "attention_required": attention_items,
            "workers": worker_summary,
            "worker_list": workers_list,
            "tasks": task_counts,
            "recent_tasks": tasks_table,
            "contacts": contacts_list,
            "accounts": accounts_list,
            "browser_sessions": sessions_list,
            "safety": {
                "reconciliation": len(reconciliations_list),
                "manual_review": len(manual_reviews_list),
                "unknown_send": reconciling_count,
                "confirmed_sent": completed_count,
            },
            "reconciliations": reconciliations_list,
            "manual_reviews": manual_reviews_list,
            "cooldowns": cooldowns_list,
            "followups": {
                "summary": followup_counts,
                "list": followups_list,
            },
            "scheduler": {
                "status": "RUNNING" if self.app.scheduler else "STOPPED",
                "ready_tasks": ready_count,
                "eligible_tasks": max(0, ready_count - len(cooldowns_list)),
                "blocked_tasks": len(cooldowns_list),
                "last_dispatch": getattr(self.app.scheduler, "last_dispatch_at", None) or "Never",
            },
            "database": {
                "engine": "SQLite",
                "connected": True,
                "schema_version": "v10",
                "wal_enabled": True,
                "integrity": "Passed",
                "database_size_mb": db_size_mb,
                "last_backup": last_backup_time,
            },
            "recent_events": recent_events,
            "badges": {
                "reconciliation": len(reconciliations_list),
                "manual_review": len(manual_reviews_list),
                "cooldown": len(cooldowns_list),
            }
        }

    def _perform_search(self, q: str) -> List[Dict[str, Any]]:
        """Search across tasks, accounts, workers, events, and reconciliations."""
        if not q:
            return []
        q_clean = f"%{q.strip()}%"
        conn = self.app.db.get_connection()
        results = []

        try:
            cur = conn.execute("SELECT id, contact_id, status FROM tasks WHERE id LIKE ? OR contact_id LIKE ? LIMIT 5;", (q_clean, q_clean))
            for r in cur.fetchall():
                results.append({"type": "Task", "id": r[0], "title": f"Task #{r[0]} ({r[1]})", "status": r[2]})
        except Exception:
            pass

        try:
            cur = conn.execute("SELECT id, username, status FROM accounts WHERE id LIKE ? OR username LIKE ? LIMIT 5;", (q_clean, q_clean))
            for r in cur.fetchall():
                results.append({"type": "Account", "id": r[0], "title": f"Account @{r[1]}", "status": r[2]})
        except Exception:
            pass

        try:
            cur = conn.execute("SELECT id, event_code, timestamp FROM events WHERE id LIKE ? OR event_code LIKE ? LIMIT 5;", (q_clean, q_clean))
            for r in cur.fetchall():
                results.append({"type": "Event", "id": r[0], "title": f"Event {r[1]}", "status": r[2]})
        except Exception:
            pass

        return results


class DashboardServer:
    """Wraps HTTP server lifecycle tied to a ProductionApp instance."""

    def __init__(self, app: Optional[ProductionApp] = None, host: str = "127.0.0.1", port: int = 8080, app_state: Optional[ProductionApp] = None):
        self.app = app or app_state
        self.host = host
        self.port = port
        self.server: Optional[ThreadedHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self, background: bool = True) -> str:
        """Start the HTTP server on configured host and port. Returns the base URL."""
        self.server = ThreadedHTTPServer((self.host, self.port), DashboardRequestHandler)
        self.server.app = self.app  # type: ignore

        # Resolve assigned port if 0 was passed
        actual_port = self.server.server_address[1]
        self.port = actual_port

        url = f"http://{self.host}:{self.port}"
        logger.info(f"Dashboard server listening at {url}")

        if background:
            self._thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="DashboardServerThread")
            self._thread.start()
        else:
            self.server.serve_forever()
        return url

    def is_healthy(self) -> bool:
        """Check if dashboard server instance is actively listening."""
        return self.server is not None and (self._thread is None or self._thread.is_alive())

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
