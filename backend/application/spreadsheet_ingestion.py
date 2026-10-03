"""Spreadsheet and Google Sheets ingestion service supporting link-based and file-based input."""

import os
import re
import csv
import io
import urllib.request
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from backend.domain.models import SourceRow
from backend.domain.enums import SourceType, TaskState, MessageState, RepliedStatus
from backend.domain.errors import ValidationError, SourceAccessError
from backend.sources.base import SourceAdapter
from backend.sources.xlsx.adapter import LocalXlsxSource, calculate_row_checksum
from backend.sources.browser_sheet.spreadsheet import SpreadsheetStructureValidator
from backend.sources.browser_sheet.adapter import BrowserSpreadsheetSource
from backend.sources.browser_sheet.driver import PlaywrightSpreadsheetDriver
from backend.application.source_service import SourceService
from backend.events.logger import get_logger

logger = get_logger("spreadsheet_ingestion")


class IngestedListSource(SourceAdapter):
    """In-memory or parsed rows adapter implementing SourceAdapter for transactional sync."""

    def __init__(self, source_identifier: str, rows: List[SourceRow]):
        super().__init__(source_identifier=source_identifier, source_type=SourceType.BROWSER_SPREADSHEET)
        self._rows = rows
        self.is_open = True

    def validate_access(self):
        from backend.domain.enums import SourceAccessStatus
        return SourceAccessStatus.ACCESSIBLE

    def open(self) -> bool:
        self.is_open = True
        return True

    def read_records(self) -> List[SourceRow]:
        return self._rows

    def update_record(self, row_index: int, updates: Dict[str, Any]) -> bool:
        return True

    def close(self) -> None:
        self.is_open = False


class SpreadsheetIngestionService:
    """
    Coordinates link-based Google Sheets, web spreadsheets, local Excel files,
    and pasted table data ingestion into the automation system.
    """

    def __init__(self, source_service: SourceService, browser_manager: Optional[Any] = None):
        self.source_service = source_service
        self.browser_manager = browser_manager

    def import_from_input(
        self,
        source_input: str,
        message_template: Optional[str] = None,
        driver: Optional[PlaywrightSpreadsheetDriver] = None,
    ) -> Dict[str, Any]:
        """
        Process arbitrary source input:
        - Google Sheets URL (https://docs.google.com/spreadsheets/d/...)
        - Web Spreadsheet URL
        - Local .xlsx or .csv filepath
        - Pasted TSV / CSV / HTML table text
        """
        cleaned = source_input.strip()
        if not cleaned:
            raise ValidationError("Source input cannot be empty.")

        # 1. Check if it's a URL
        if cleaned.startswith("http://") or cleaned.startswith("https://"):
            return self._import_from_url(cleaned, message_template=message_template, driver=driver)

        # 2. Check if it's a local file path
        potential_path = Path(cleaned)
        if potential_path.exists() and potential_path.is_file():
            return self._import_from_file(str(potential_path), message_template=message_template)

        # 3. Otherwise treat as pasted table text / HTML / TSV
        return self._import_from_raw_text(cleaned, source_name="Pasted Table Input", message_template=message_template)

    def _import_from_url(
        self,
        url: str,
        message_template: Optional[str] = None,
        driver: Optional[PlaywrightSpreadsheetDriver] = None,
    ) -> Dict[str, Any]:
        """Import from Google Sheets or web spreadsheet URL."""
        # Check if Google Sheets
        gsheet_match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
        if gsheet_match:
            sheet_id = gsheet_match.group(1)
            gid_match = re.search(r"[#&?]gid=([0-9]+)", url)
            gid_part = f"&gid={gid_match.group(1)}" if gid_match else ""
            export_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv{gid_part}"

            # Attempt public export download first
            try:
                req = urllib.request.Request(
                    export_url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status == 200:
                        content_type = resp.headers.get("Content-Type", "")
                        if "text/csv" in content_type or "text/plain" in content_type or "application" in content_type:
                            csv_text = resp.read().decode("utf-8", errors="replace")
                            if csv_text.strip() and not ("<!DOCTYPE html>" in csv_text or "<html" in csv_text.lower()):
                                logger.info(f"Successfully fetched CSV export for Google Sheet {sheet_id}")
                                return self._import_from_raw_text(
                                    csv_text,
                                    source_name=f"Google Sheets ({sheet_id})",
                                    message_template=message_template,
                                )
            except Exception as e:
                logger.warning(f"Direct Google Sheets export not available ({e}); falling back to browser driver.")

        # Fallback or general web spreadsheet: use BrowserSpreadsheetSource with Playwright driver
        effective_driver = driver
        if not effective_driver and self.browser_manager:
            try:
                # Use default session from browser manager if available
                session = self.browser_manager.get_session("DEFAULT") or self.browser_manager.start_session("SPREADSHEET_IMPORT")
                if session:
                    effective_driver = PlaywrightSpreadsheetDriver(session=session)
            except Exception as e:
                logger.warning(f"Could not initialize browser session for spreadsheet: {e}")

        if not effective_driver:
            # Raise clear actionable error
            raise SourceAccessError(
                f"Cannot access spreadsheet at '{url}'. The Google Sheet is either private or requires browser access. "
                "Ensure the sheet link sharing is set to 'Anyone with the link can view' or start the browser agent.",
                code="SOURCE_UNAVAILABLE",
            )

        adapter = BrowserSpreadsheetSource(spreadsheet_url=url, driver=effective_driver)
        sync_run = self.source_service.sync_source(adapter)
        return self._summarize_sync(sync_run)

    def _import_from_file(self, file_path: str, message_template: Optional[str] = None) -> Dict[str, Any]:
        """Import from local .xlsx or .csv file."""
        path = Path(file_path)
        if path.suffix.lower() == ".xlsx":
            adapter = LocalXlsxSource(str(path))
            sync_run = self.source_service.sync_source(adapter)
            return self._summarize_sync(sync_run)
        elif path.suffix.lower() == ".csv":
            text = path.read_text(encoding="utf-8", errors="replace")
            return self._import_from_raw_text(text, source_name=path.name, message_template=message_template)
        else:
            raise ValidationError(f"Unsupported spreadsheet file extension: '{path.suffix}'. Must be .xlsx or .csv")

    def _import_from_raw_text(
        self,
        raw_text: str,
        source_name: str = "Raw Input",
        message_template: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Parse raw CSV, TSV, or HTML table string into rows and sync."""
        lines = [line.strip() for line in raw_text.strip().splitlines() if line.strip()]
        if not lines:
            raise ValidationError("Spreadsheet data is empty.")

        # Check delimiter: tab vs comma
        delimiter = "\t" if "\t" in lines[0] else ","
        reader = csv.reader(io.StringIO(raw_text), delimiter=delimiter)
        raw_rows = [row for row in reader if any(c.strip() for c in row)]

        if not raw_rows:
            raise ValidationError("Could not parse any rows from spreadsheet input.")

        # Extract headers and validate
        header_row = [str(c).strip() for c in raw_rows[0]]
        canonical_map, col_map = SpreadsheetStructureValidator.validate_headers(header_row)

        source_rows: List[SourceRow] = []
        for idx, row_cells in enumerate(raw_rows[1:], start=2):
            parsed = SpreadsheetStructureValidator.parse_row(
                row_cells=row_cells,
                row_index=idx,
                idx_to_canonical=col_map,
            )
            if parsed:
                if message_template and "{name}" in message_template:
                    parsed.message = message_template.replace("{name}", parsed.name)
                elif message_template and not parsed.message:
                    parsed.message = message_template
                source_rows.append(parsed)

        if not source_rows:
            raise ValidationError("No valid contact rows with Instagram link/username found in spreadsheet.")

        adapter = IngestedListSource(source_identifier=source_name, rows=source_rows)
        sync_run = self.source_service.sync_source(adapter)
        return self._summarize_sync(sync_run)

    def _summarize_sync(self, sync_run: Any) -> Dict[str, Any]:
        """Calculate counts of tasks created in READY, COMPLETED, and SKIPPED states."""
        task_repo = self.source_service.task_repo
        ready_tasks = task_repo.list_by_status(TaskState.READY) if hasattr(task_repo, "list_by_status") else []
        
        # Query database directly for exact task state counts
        db = getattr(self.source_service.contact_repo, "db", None)
        ready_count = 0
        completed_count = 0
        skipped_count = 0
        if db:
            conn = db.get_connection()
            c = conn.cursor()
            ready_count = c.execute("SELECT count(*) FROM tasks WHERE status = 'READY'").fetchone()[0]
            completed_count = c.execute("SELECT count(*) FROM tasks WHERE status = 'COMPLETED'").fetchone()[0]
            skipped_count = c.execute("SELECT count(*) FROM tasks WHERE status = 'SKIPPED'").fetchone()[0]

        return {
            "success": True,
            "sync_code": getattr(sync_run, "sync_code", ""),
            "records_read": getattr(sync_run, "records_read", 0),
            "records_written": getattr(sync_run, "records_written", 0),
            "ready": ready_count,
            "completed": completed_count,
            "skipped": skipped_count,
            "message": (
                f"Import complete: {getattr(sync_run, 'records_read', 0)} rows processed. "
                f"{ready_count} pending leads queued (READY), "
                f"{completed_count} previously completed (Done), "
                f"{skipped_count} skipped (Closed/Unreachable)."
            ),
        }
