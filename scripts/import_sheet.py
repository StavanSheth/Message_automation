"""CLI script to import leads from Google Sheets link, web spreadsheet, or local Excel file."""

import sys
import argparse
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.bootstrap import build_production_app
from backend.application.spreadsheet_ingestion import SpreadsheetIngestionService


def main():
    parser = argparse.ArgumentParser(description="Import leads spreadsheet into Message Automation")
    parser.add_argument("--url", type=str, default=None, help="Google Sheets or web spreadsheet URL")
    parser.add_argument("--file", type=str, default=None, help="Local .xlsx or .csv file path")
    parser.add_argument("--template", type=str, default=None, help="Custom message template (e.g. 'Hello {name}!')")
    parser.add_argument("--db", type=str, default="data/app.db", help="Path to database")
    args = parser.parse_args()

    source_input = args.url or args.file
    if not source_input:
        print("Error: Must provide either --url or --file")
        sys.exit(1)

    print(f"Connecting to database at {args.db}...")
    app = build_production_app(db_path=args.db)

    print(f"Importing leads from: {source_input}...")
    ingestion = app.spreadsheet_ingestion_service or SpreadsheetIngestionService(
        source_service=app.source_service,
        browser_manager=app.browser_manager,
    )

    try:
        result = ingestion.import_from_input(source_input=source_input, message_template=args.template)
        print("\n" + "=" * 60)
        print("  IMPORT SUCCESSFUL!")
        print("=" * 60)
        print(f"  Records processed: {result.get('records_read', 0)}")
        print(f"  Pending ready tasks: {result.get('ready', 0)}")
        print(f"  Already completed (Done): {result.get('completed', 0)}")
        print(f"  Skipped (Closed/Unreachable): {result.get('skipped', 0)}")
        print(f"  Details: {result.get('message', '')}")
        print("=" * 60 + "\n")
    except Exception as e:
        print(f"\nImport failed: {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
