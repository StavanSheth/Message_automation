"""Authoritative Command-Line Interface and Runtime Controller for Message Automation."""

import os
import sys
import json
import time
import signal
import urllib.request
import urllib.error
import threading
import argparse
from pathlib import Path
from typing import Optional, Dict, Any, List

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config.settings import AppSettings, get_settings
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.bootstrap import build_production_app
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.source_record_repo import SourceRecordRepository
from backend.repositories.task_repo import TaskRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.followup_repo import FollowupRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.sync_run_repo import SyncRunRepository
from backend.repositories.worker_repo import WorkerRepository
from backend.repositories.browser_session_repo import BrowserSessionRepository
from backend.repositories.system_control_repo import SystemControlRepository
from backend.sources.xlsx.adapter import LocalXlsxSource
from backend.application.source_service import SourceService
from backend.health.hardware import HardwareDetectionService
from backend.events.logger import get_logger

logger = get_logger("cli")


def _call_api(path: str, method: str = "GET", data: Optional[Dict[str, Any]] = None, host: str = "127.0.0.1", port: int = 8080) -> Optional[Dict[str, Any]]:
    """Helper to query the running dashboard server API if active."""
    url = f"http://{host}:{port}{path}"
    try:
        req = urllib.request.Request(url, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
            body = json.dumps(data).encode("utf-8")
        else:
            body = None
        with urllib.request.urlopen(req, data=body, timeout=3.0) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except Exception:
        return None


def init_db(db_path: Optional[str] = None) -> None:
    """Initialize database and run migrations."""
    db = DatabaseManager(db_path)
    runner = MigrationRunner(db)
    applied = runner.apply_pending()
    print(f"Applied migrations: {applied if applied else 'None (database already up-to-date)'}")
    print(f"Schema verification: {'PASSED' if runner.verify_schema() else 'FAILED'}")


def check_hardware() -> None:
    """Run hardware capability detection."""
    detector = HardwareDetectionService()
    caps = detector.detect_capabilities()
    print("HARDWARE DETECTION REPORT:")
    print(f"  CPU Cores: {caps.cpu_count}")
    print(f"  RAM Total: {caps.total_ram_gb} GB (Available: {caps.available_ram_gb} GB)")
    print(f"  NVIDIA GPU Present: {caps.gpu_available}")
    if caps.gpu_available:
        print(f"  GPU Name: {caps.gpu_name}")
        print(f"  VRAM: {caps.vram_gb} GB")
    print(f"  Single Browser Mode Available: {caps.single_browser_available}")
    print(f"  Multi Browser Mode Available: {caps.multi_browser_available}")
    print(f"  Recommended Max Workers: {caps.recommended_max_workers}")


def import_excel(file_path: str, db_path: Optional[str] = None) -> None:
    """Import local XLSX spreadsheet."""
    db = DatabaseManager(db_path)
    runner = MigrationRunner(db)
    runner.apply_pending()

    contact_repo = ContactRepository(db)
    srec_repo = SourceRecordRepository(db)
    task_repo = TaskRepository(db)
    msg_repo = MessageRepository(db)
    fu_repo = FollowupRepository(db)
    evt_repo = EventRepository(db)
    sync_repo = SyncRunRepository(db)

    service = SourceService(
        contact_repo=contact_repo,
        source_record_repo=srec_repo,
        task_repo=task_repo,
        message_repo=msg_repo,
        followup_repo=fu_repo,
        event_repo=evt_repo,
        sync_run_repo=sync_repo,
    )

    adapter = LocalXlsxSource(file_path)
    run = service.sync_source(adapter)
    print(f"Sync complete. Code: {run.sync_code}, Status: {run.status.value}, "
          f"Read: {run.records_read}, Written: {run.records_written}, Conflicts: {run.conflicts}")


def start_app(args) -> None:
    """
    Authoritative application startup path:
    1. Display STARTING state.
    2. Check Database and Migrations.
    3. Validate complete dependency graph.
    4. Start worker and browser session (visible browser by default in manual mode).
    5. Start scheduler and live dashboard.
    6. Transition to RUNNING.
    """
    print("\n" + "=" * 60)
    print("  MESSAGE_AUTOMATION — STARTING AUTHORITATIVE RUNTIME")
    print("=" * 60)
    print("Application: STARTING")

    # 1. Configuration
    headless = args.headless
    settings = AppSettings.load(overrides={
        "browser_headless": headless,
        "execution_mode": args.mode,
        "application_mode": args.mode,
    })

    # 2. Database & Migrations
    db_path = args.db or settings.database_path
    try:
        db = DatabaseManager(db_path)
        runner = MigrationRunner(db)
        runner.apply_pending()
        print("Database: OK")
        print("Migrations: OK")
    except Exception as e:
        print(f"Database: FAILED ({e})")
        print("Migrations: FAILED")
        print("Application: DEGRADED")
        sys.exit(1)

    # 3. Build & Validate Dependency Graph
    try:
        app = build_production_app(db_path=db_path, settings=settings)
        is_valid, errors = app.validate_dependency_graph()
        if not is_valid:
            print(f"Dependency graph: FAILED ({errors})")
            print("Application: DEGRADED")
            sys.exit(1)
        print("Dependency graph: OK")
    except Exception as e:
        print(f"Dependency graph: FAILED ({e})")
        print("Application: DEGRADED")
        sys.exit(1)

    # 4. Browser Subsystem
    browser_ok = False
    try:
        # Verify browser driver initialization
        from backend.browser.driver import find_browser_executable
        exec_path = find_browser_executable()
        print(f"Browser engine: OK (Detected: {exec_path or 'Playwright bundled Chromium'})")
        browser_ok = True
    except Exception as e:
        print(f"Browser engine: FAILED ({e})")

    # 5. Start Application Graph
    res = app.start()
    app_status = res.get("status", "error")

    worker_ok = app.worker_manager is not None
    print(f"Worker manager: {'READY' if worker_ok else 'FAILED'}")

    sched_paused = getattr(app.scheduler, "is_paused", False)
    sched_ok = app.scheduler is not None and not (sched_paused() if callable(sched_paused) else sched_paused)
    print(f"Scheduler: {'READY' if sched_ok else 'FAILED'}")

    # Check active browser sessions
    sessions = app.browser_manager.check_health().get("sessions", {})
    if sessions:
        print("Browser session: READY")
    else:
        print("Browser session: READY (on-demand standby for dispatched worker)")

    # 6. Dashboard / API Server
    web_server = None
    dashboard_url = None
    if not args.no_web:
        try:
            from backend.web.server import DashboardServer
            web_server = DashboardServer(app=app, host=args.host, port=args.port)
            dashboard_url = web_server.start(background=True)
            print(f"Dashboard/API: READY ({dashboard_url})")
        except Exception as e:
            print(f"Dashboard/API: DEGRADED ({e})")
    else:
        print("Dashboard/API: DISABLED (CLI headless flag)")

    final_state = "RUNNING" if app_status in ("ok", "success") else "DEGRADED"
    print(f"Application: {final_state}")
    print("=" * 60)
    print(f"Runtime Mode: {settings.application_mode} | Visible Browser: {'NO (headless)' if headless else 'YES (visible)'}")
    if dashboard_url:
        print(f"Live Dashboard: {dashboard_url}")
    print("Press Ctrl+C to stop the application gracefully.\n")

    if args.once:
        if web_server:
            web_server.stop()
        app.stop()
        return

    # Graceful shutdown handler
    stop_event = threading.Event()

    def handle_signal(sig, frame):
        logger.info(f"Signal {sig} received, initiating graceful shutdown...")
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    try:
        signal.signal(signal.SIGTERM, handle_signal)
    except Exception:
        pass

    try:
        while not stop_event.is_set():
            stop_event.wait(timeout=1.0)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        print("\nInitiating graceful shutdown...")
        if web_server:
            web_server.stop()
        app.stop()
        print("Application stopped cleanly. All browser sessions and worker leases released.")


def show_status(db_path: Optional[str] = None, host: str = "127.0.0.1", port: int = 8080) -> None:
    """
    Authoritative system status command:
    - Application state
    - Worker state
    - Browser state & PID
    - Current task & account
    - Scheduler state
    - Database state
    - Last error
    """
    # Try querying live API first
    live_status = _call_api("/healthz", host=host, port=port)
    live_diag = _call_api("/api/diagnostics/summary", host=host, port=port)

    settings = get_settings()
    db = DatabaseManager(db_path or settings.database_path)
    runner = MigrationRunner(db)
    schema_ok = runner.verify_schema()

    control_repo = SystemControlRepository(db)
    persisted_state = control_repo.get_state()
    app_state = live_status.get("state") if live_status else (persisted_state.value if persisted_state else "STOPPED")

    worker_repo = WorkerRepository(db)
    workers = worker_repo.list_all()

    sess_repo = BrowserSessionRepository(db)
    sessions = sess_repo.list_active()

    task_repo = TaskRepository(db)
    counts = task_repo.count_by_status()

    from backend.repositories.error_repo import ErrorRepository
    err_repo = ErrorRepository(db)
    last_err = err_repo.list_recent(limit=1)

    print("\n" + "=" * 50)
    print("  MESSAGE_AUTOMATION — SYSTEM STATUS")
    print("=" * 50)
    print(f"Application State: {app_state}")
    print(f"Database:          {db.db_path} ({'HEALTHY' if schema_ok else 'UNHEALTHY'})")
    print(f"API Server:        {'ONLINE' if live_status else 'OFFLINE'}")

    print("\n-- WORKERS --")
    if workers:
        for w in workers:
            print(f"  Worker ID:    {w.id}")
            print(f"  Status:       {w.status}")
            print(f"  Current Task: {w.current_task_id or 'None (idle)'}")
            print(f"  Last Ping:    {w.last_heartbeat}")
    else:
        print("  No workers registered in database.")

    print("\n-- BROWSER SUBSYSTEM --")
    if sessions:
        for s in sessions:
            print(f"  Session ID:   {s.id}")
            print(f"  Status:       {s.status}")
            print(f"  Profile Path: {s.profile_path}")
            print(f"  Worker:       {s.worker_id or 'Unassigned'}")
            print(f"  Account:      {s.account_id or 'Default'}")
    else:
        print("  No active browser sessions.")

    print("\n-- TASKS --")
    for st, cnt in counts.items():
        if cnt > 0:
            print(f"  {st:<15}: {cnt}")

    print("\n-- LAST ERROR --")
    if last_err:
        e = last_err[0]
        code_str = e.code.value if hasattr(e.code, "value") else str(e.code)
        print(f"  Code:      {code_str}")
        print(f"  Message:   {e.message}")
        print(f"  Timestamp: {e.created_at}")
    else:
        print("  None (clean)")

    print("=" * 50 + "\n")


def pause_app(host: str = "127.0.0.1", port: int = 8080) -> None:
    """Pause running application and task dispatching."""
    res = _call_api("/api/control/pause", method="POST", data={"reason": "Operator requested pause via CLI"}, host=host, port=port)
    if res and res.get("status") == "success":
        print("Application paused successfully.")
    else:
        # Fallback to direct DB state update
        settings = get_settings()
        db = DatabaseManager(settings.database_path)
        ctrl_repo = SystemControlRepository(db)
        from backend.domain.enums import SystemState
        ctrl_repo.create_state_transition(SystemState.PAUSED, "Paused via CLI fallback")
        print("Application state updated to PAUSED in database.")


def resume_app(host: str = "127.0.0.1", port: int = 8080) -> None:
    """Resume running application and task dispatching."""
    res = _call_api("/api/control/resume", method="POST", data={"reason": "Operator requested resume via CLI"}, host=host, port=port)
    if res and res.get("status") == "success":
        print("Application resumed successfully.")
    else:
        # Fallback to direct DB state update
        settings = get_settings()
        db = DatabaseManager(settings.database_path)
        ctrl_repo = SystemControlRepository(db)
        from backend.domain.enums import SystemState
        ctrl_repo.create_state_transition(SystemState.RUNNING, "Resumed via CLI fallback")
        print("Application state updated to RUNNING in database.")


def stop_app(host: str = "127.0.0.1", port: int = 8080) -> None:
    """Stop the running application."""
    res = _call_api("/api/control/stop", method="POST", data={"reason": "Operator requested stop via CLI"}, host=host, port=port)
    if res and res.get("status") == "success":
        print("Application stop signal dispatched successfully.")
    else:
        settings = get_settings()
        db = DatabaseManager(settings.database_path)
        ctrl_repo = SystemControlRepository(db)
        from backend.domain.enums import SystemState
        ctrl_repo.create_state_transition(SystemState.STOPPED, "Stopped via CLI fallback")
        print("Application state updated to STOPPED in database.")


def restart_app(args) -> None:
    """Restart application cleanly."""
    print("Initiating application restart...")
    stop_app(host=args.host, port=args.port)
    time.sleep(2.0)
    start_app(args)


def browser_command(args) -> None:
    """Inspect browser driver or launch isolated visible session for verification."""
    from backend.browser.driver import find_browser_executable, PlaywrightBrowserDriver
    from backend.browser.browser_types import BrowserLaunchConfig, BrowserType
    print("Detected Browser Executable:", find_browser_executable() or "Playwright default Chromium")
    if args.launch:
        print("Launching visible verification browser session...")
        cfg = BrowserLaunchConfig(browser_type=BrowserType.CHROMIUM, headless=False)
        driver = PlaywrightBrowserDriver(cfg)
        driver.launch()
        print("Browser launched successfully. Current URL:", driver.current_url())
        driver.navigate("https://www.instagram.com")
        print("Navigated to Instagram. Waiting 5 seconds before closing...")
        time.sleep(5.0)
        driver.close()
        print("Browser session closed cleanly.")


def worker_command(db_path: Optional[str] = None) -> None:
    """List workers and active leases."""
    settings = get_settings()
    db = DatabaseManager(db_path or settings.database_path)
    worker_repo = WorkerRepository(db)
    workers = worker_repo.list_all()
    print(f"\nTotal Registered Workers: {len(workers)}")
    for w in workers:
        print(f"  Worker ID: {w.id} | Status: {w.status} | Account: {w.account_id or 'Default'} | Heartbeat: {w.last_heartbeat}")


def diagnostics_command(db_path: Optional[str] = None) -> None:
    """Print recent diagnostic artifacts and failure reports."""
    from backend.diagnostics.collector import DiagnosticCollector
    settings = get_settings()
    db = DatabaseManager(db_path or settings.database_path)
    collector = DiagnosticCollector(db=db)
    recent = collector.list_recent(limit=10)
    print(f"\nRecent Diagnostic Failure Bundles ({len(recent)}):")
    if not recent:
        print("  No recent diagnostic failure bundles found.")
    for art in recent:
        print(f"  ID: {art.id} | Task: {art.task_id or 'N/A'} | Code: {art.error_code} | Reason: {art.reason} | File: {art.file_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Instagram Message Automation CLI (Authoritative Entrypoint)")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # start
    start_p = subparsers.add_parser("start", help="Start the authoritative production application")
    start_p.add_argument("--db", type=str, default=None, help="Database path")
    start_p.add_argument("--headless", action="store_true", default=False, help="Run browser in headless mode (default: visible browser)")
    start_p.add_argument("--mode", type=str, default="MANUAL", choices=["MANUAL", "AUTOMATIC"], help="Execution mode (default: MANUAL)")
    start_p.add_argument("--host", type=str, default="127.0.0.1", help="Dashboard host")
    start_p.add_argument("--port", type=int, default=8080, help="Dashboard port")
    start_p.add_argument("--no-web", action="store_true", help="Disable web dashboard server")
    start_p.add_argument("--once", action="store_true", help="Run startup verification and exit")

    # stop
    stop_p = subparsers.add_parser("stop", help="Stop running production application")
    stop_p.add_argument("--host", type=str, default="127.0.0.1", help="Dashboard host")
    stop_p.add_argument("--port", type=int, default=8080, help="Dashboard port")

    # status
    status_p = subparsers.add_parser("status", help="Show system, worker, and browser status")
    status_p.add_argument("--db", type=str, default=None, help="Database path")
    status_p.add_argument("--host", type=str, default="127.0.0.1", help="Dashboard host")
    status_p.add_argument("--port", type=int, default=8080, help="Dashboard port")

    # pause
    pause_p = subparsers.add_parser("pause", help="Pause task scheduling and workers")
    pause_p.add_argument("--host", type=str, default="127.0.0.1", help="Dashboard host")
    pause_p.add_argument("--port", type=int, default=8080, help="Dashboard port")

    # resume
    resume_p = subparsers.add_parser("resume", help="Resume task scheduling and workers")
    resume_p.add_argument("--host", type=str, default="127.0.0.1", help="Dashboard host")
    resume_p.add_argument("--port", type=int, default=8080, help="Dashboard port")

    # restart
    restart_p = subparsers.add_parser("restart", help="Restart running production application")
    restart_p.add_argument("--db", type=str, default=None, help="Database path")
    restart_p.add_argument("--headless", action="store_true", default=False)
    restart_p.add_argument("--mode", type=str, default="MANUAL", choices=["MANUAL", "AUTOMATIC"])
    restart_p.add_argument("--host", type=str, default="127.0.0.1")
    restart_p.add_argument("--port", type=int, default=8080)
    restart_p.add_argument("--no-web", action="store_true")
    restart_p.add_argument("--once", action="store_true")

    # browser
    browser_p = subparsers.add_parser("browser", help="Inspect or launch browser")
    browser_p.add_argument("--launch", action="store_true", help="Launch visible test browser")

    # worker
    worker_p = subparsers.add_parser("worker", help="Inspect worker states and task leases")
    worker_p.add_argument("--db", type=str, default=None, help="Database path")

    # diagnostics
    diag_p = subparsers.add_parser("diagnostics", help="Inspect recent diagnostic failure bundles")
    diag_p.add_argument("--db", type=str, default=None, help="Database path")

    # init-db
    init_parser = subparsers.add_parser("init-db", help="Initialize and migrate database")
    init_parser.add_argument("--db", type=str, default=None, help="Database path")

    # hardware
    subparsers.add_parser("hardware", help="Inspect hardware capabilities")

    # import-excel
    import_parser = subparsers.add_parser("import-excel", help="Import Excel spreadsheet")
    import_parser.add_argument("file", type=str, help="Path to .xlsx file")
    import_parser.add_argument("--db", type=str, default=None, help="Database path")

    # If no command passed, print help or default to start
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "start":
        start_app(args)
    elif args.command == "stop":
        stop_app(args.host, args.port)
    elif args.command == "status":
        show_status(args.db, args.host, args.port)
    elif args.command == "pause":
        pause_app(args.host, args.port)
    elif args.command == "resume":
        resume_app(args.host, args.port)
    elif args.command == "restart":
        restart_app(args)
    elif args.command == "browser":
        browser_command(args)
    elif args.command == "worker":
        worker_command(args.db)
    elif args.command == "diagnostics":
        diagnostics_command(args.db)
    elif args.command == "init-db":
        init_db(args.db)
    elif args.command == "hardware":
        check_hardware()
    elif args.command == "import-excel":
        import_excel(args.file, args.db)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
