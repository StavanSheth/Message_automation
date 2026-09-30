"""Command-line interface for Phase 1 database management, source import, and status."""

import argparse
import json
import sys
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config.settings import get_settings
from backend.database.manager import DatabaseManager
from backend.database.migrations import MigrationRunner
from backend.repositories.contact_repo import ContactRepository
from backend.repositories.source_record_repo import SourceRecordRepository
from backend.repositories.task_repo import TaskRepository
from backend.repositories.message_repo import MessageRepository
from backend.repositories.followup_repo import FollowupRepository
from backend.repositories.event_repo import EventRepository
from backend.repositories.sync_run_repo import SyncRunRepository
from backend.sources.xlsx.adapter import LocalXlsxSource
from backend.application.source_service import SourceService
from backend.health.hardware import HardwareDetectionService


def init_db(db_path: str = None) -> None:
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


def import_excel(file_path: str, db_path: str = None) -> None:
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


def print_status(db_path: str = None) -> None:
    """Print system and database status."""
    db = DatabaseManager(db_path)
    runner = MigrationRunner(db)
    print(f"Database: {db.db_path}")
    print(f"Schema valid: {runner.verify_schema()}")

    contact_repo = ContactRepository(db)
    task_repo = TaskRepository(db)
    fu_repo = FollowupRepository(db)

    print(f"Total contacts: {contact_repo.count()}")
    print("Tasks by status:")
    for status, count in task_repo.count_by_status().items():
        print(f"  {status}: {count}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Instagram Browser Automation CLI")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    init_parser = subparsers.add_parser("init-db", help="Initialize and migrate database")
    init_parser.add_argument("--db", type=str, default=None, help="Database path")

    subparsers.add_parser("hardware", help="Inspect hardware capabilities")

    import_parser = subparsers.add_parser("import-excel", help="Import Excel spreadsheet")
    import_parser.add_argument("file", type=str, help="Path to .xlsx file")
    import_parser.add_argument("--db", type=str, default=None, help="Database path")

    status_parser = subparsers.add_parser("status", help="Show system status")
    status_parser.add_argument("--db", type=str, default=None, help="Database path")

    args = parser.parse_args()

    if args.command == "init-db":
        init_db(args.db)
    elif args.command == "hardware":
        check_hardware()
    elif args.command == "import-excel":
        import_excel(args.file, args.db)
    elif args.command == "status":
        print_status(args.db)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
