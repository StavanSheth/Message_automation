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
        """Import from Google Sheets, OneDrive, Excel Online, or web spreadsheet URL."""
        # 1. Check if Google Sheets direct export is available
        gsheet_match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
        if gsheet_match:
            sheet_id = gsheet_match.group(1)
            gid_match = re.search(r"[#&?]gid=([0-9]+)", url)
            gid_part = f"&gid={gid_match.group(1)}" if gid_match else ""
            export_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv{gid_part}"

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
                logger.warning(f"Direct Google Sheets export not available ({e}); falling back to browser.")

        # 2. General Web Spreadsheet (OneDrive / Excel Online / Google Sheets browser):
        # Fetch using Playwright browser automation
        try:
            logger.info(f"Accessing spreadsheet URL via browser session: {url}")
            csv_text = self._fetch_via_browser(url)
            if csv_text and csv_text.strip():
                return self._import_from_raw_text(
                    csv_text,
                    source_name=url,
                    message_template=message_template,
                )
        except Exception as e:
            logger.warning(f"Browser-based spreadsheet fetch encountered issue: {e}")

        # Fallback to driver if provided
        effective_driver = driver
        if effective_driver:
            adapter = BrowserSpreadsheetSource(spreadsheet_url=url, driver=effective_driver)
            sync_run = self.source_service.sync_source(adapter)
            return self._summarize_sync(sync_run)

        raise SourceAccessError(
            f"Cannot access spreadsheet at '{url}'. The spreadsheet is either private or requires browser authentication. "
            "Ensure the sharing link is set to 'Anyone with the link can view' or open via browser.",
            code="SOURCE_UNAVAILABLE",
        )

    def _fetch_via_browser(self, url: str) -> str:
        """
        Use Playwright browser session to open web spreadsheets (OneDrive, Excel Online, Google Sheets)
        and retrieve CSV data directly via real browser automation.
        """
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                page.goto(url, wait_until="networkidle", timeout=35000)
            except Exception:
                page.wait_for_timeout(4000)

            page.wait_for_timeout(5000)

            # Check if Excel Online WAC frame is present
            frame = page.frame(name="WacFrame_Excel_0")
            if not frame:
                for f in page.frames:
                    if "officeapps.live.com" in f.url or "xlviewer" in f.url:
                        frame = f
                        break

            if frame:
                try:
                    # Click File menu -> Export -> Download as CSV
                    file_btn = frame.query_selector("#FileMenuFlyoutLauncher") or frame.query_selector("text=File")
                    if file_btn:
                        file_btn.click()
                        page.wait_for_timeout(1000)

                    export_btn = frame.query_selector("text=Export")
                    if export_btn:
                        export_btn.click()
                        page.wait_for_timeout(1500)

                    csv_dl_btn = frame.query_selector("text=Download as CSV") or frame.query_selector("text=Download as CSV UTF-8")
                    if csv_dl_btn:
                        with page.expect_download(timeout=15000) as download_info:
                            csv_dl_btn.click()
                        download = download_info.value
                        os.makedirs("data", exist_ok=True)
                        dest_path = os.path.join("data", "imported_excel.csv")
                        download.save_as(dest_path)
                        with open(dest_path, "r", encoding="utf-8-sig", errors="replace") as f:
                            csv_content = f.read()
                        browser.close()
                        return csv_content
                except Exception as e:
                    logger.warning(f"Excel Online export failed via frame: {e}")

            # Fallback to extracting table or grid text from DOM
            all_frames = [page] + list(page.frames)
            for f in all_frames:
                try:
                    tables = f.evaluate("""() => {
                        const trs = Array.from(document.querySelectorAll('table tr'));
                        if (trs.length > 1) {
                            return trs.map(tr => Array.from(tr.querySelectorAll('th, td')).map(c => (c.innerText || '').trim()).join('\\t')).join('\\n');
                        }
                        const rows = Array.from(document.querySelectorAll('[role="row"]'));
                        if (rows.length > 1) {
                            return rows.map(r => Array.from(r.querySelectorAll('[role="gridcell"], [role="columnheader"]')).map(c => (c.innerText || '').trim()).join('\\t')).join('\\n');
                        }
                        return '';
                    }""")
                    if tables and ("instagram" in tables.lower() or "client" in tables.lower() or "industry" in tables.lower()):
                        browser.close()
                        return tables
                except Exception:
                    continue

            browser.close()
            raise SourceAccessError(f"Could not extract spreadsheet data from browser session for '{url}'")

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

        # Extract headers and validate, scanning first 5 rows to skip title banners if needed
        canonical_map = None
        col_map = None
        header_idx = 0
        for i in range(min(5, len(raw_rows))):
            cand = [str(c).strip() for c in raw_rows[i]]
            try:
                canonical_map, col_map = SpreadsheetStructureValidator.validate_headers(cand)
                header_idx = i
                break
            except Exception:
                continue

        if not canonical_map or not col_map:
            # Fallback to validating the first row so detailed error is raised
            header_row = [str(c).strip() for c in raw_rows[0]]
            canonical_map, col_map = SpreadsheetStructureValidator.validate_headers(header_row)

        source_rows: List[SourceRow] = []
        for idx, row_cells in enumerate(raw_rows[header_idx + 1:], start=header_idx + 2):
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
