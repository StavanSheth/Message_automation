"""Authoritative production entry point for Message Automation."""

import sys
import time
import signal
import threading
import argparse
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.bootstrap import build_production_app
from backend.events.logger import get_logger

logger = get_logger("main")


def main() -> None:
    parser = argparse.ArgumentParser(description="Instagram Message Automation Production Service")
    parser.add_argument("--db", type=str, default=None, help="Database path")
    parser.add_argument("--check-graph", action="store_true", help="Validate dependency graph and exit")
    parser.add_argument("--once", action="store_true", help="Start and exit immediately without keeping server active")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Web dashboard server host")
    parser.add_argument("--port", type=int, default=8080, help="Web dashboard server port")
    parser.add_argument("--no-web", action="store_true", help="Disable web dashboard server")
    parser.add_argument("--headless", action="store_true", default=False, help="Run browser in headless mode (default: visible browser)")
    parser.add_argument("--mode", type=str, default="AUTOMATIC", choices=["MANUAL", "AUTOMATIC"], help="Execution mode (default: AUTOMATIC)")
    args = parser.parse_args()

    from backend.config.settings import AppSettings
    app_settings = AppSettings.load(overrides={
        "browser_headless": args.headless,
        "execution_mode": args.mode,
    })
    app = build_production_app(db_path=args.db, settings=app_settings)

    is_valid, errors = app.validate_dependency_graph()
    if not is_valid:
        logger.error(f"Dependency graph validation failed: {errors}")
        print(f"FAILED: Dependency graph invalid: {errors}")
        sys.exit(1)

    print("Dependency graph verified: PASSED")
    if args.check_graph:
        sys.exit(0)

    res = app.start()
    print(f"Application start result: {res}")

    # Ensure visible browser opens Instagram and evaluates auth state on startup
    if getattr(app, "interactive_startup_service", None):
        try:
            startup_info = app.interactive_startup_service.launch_interactive_session()
            print(f"Browser Startup: {startup_info.get('status')} | Engine: {startup_info.get('browser_type')} | PID: {startup_info.get('pid')} | Auth: {startup_info.get('auth_status')}")
        except Exception as ex:
            logger.warning(f"Interactive browser startup encountered non-fatal error: {ex}")

    if not args.once:
        web_server = None
        if not args.no_web:
            try:
                from backend.web.server import DashboardServer
                web_server = DashboardServer(app=app, host=args.host, port=args.port)
                dashboard_url = web_server.start(background=True)
                print(f"\n=======================================================")
                print(f"  Live Operational Dashboard: {dashboard_url}")
                print(f"=======================================================\n")
            except Exception as e:
                logger.warning(f"Could not start dashboard server: {e}")

        print("Production server running. Press Ctrl+C to terminate.")
        stop_event = threading.Event()

        def handle_signal(sig, frame):
            logger.info(f"Signal {sig} received, stopping server...")
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
            print("Shutting down production server...")
            if web_server:
                web_server.stop()
            app.stop()
            print("Production server stopped.")


if __name__ == "__main__":
    main()

