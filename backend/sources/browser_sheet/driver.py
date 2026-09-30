"""Browser spreadsheet driver protocol and Playwright implementation."""

from typing import Protocol, List, Dict, Any, Optional
import time
import re

from backend.domain.models import SourceRow
from backend.domain.enums import SourceAccessStatus, ErrorCode
from backend.domain.errors import SourceAccessError, ValidationError, ConflictError
from backend.browser.session import BrowserSessionInstance
from backend.browser.driver import BrowserDriver
from backend.browser.exceptions import BrowserException, BrowserTimeoutError, BrowserNavigationError
from backend.sources.browser_sheet.validators import validate_spreadsheet_url, UrlValidationResult
from backend.sources.browser_sheet.spreadsheet import SpreadsheetStructureValidator
from backend.events.logger import get_logger

logger = get_logger("browser_spreadsheet_driver")


class BrowserSpreadsheetDriver(Protocol):
    """Protocol for browser spreadsheet interaction."""

    def check_access(self, url: str) -> SourceAccessStatus:
        ...

    def open(self, url: str) -> bool:
        ...

    def read_sheet(self, url: str) -> List[SourceRow]:
        ...

    def read_headers(self, url: str) -> List[str]:
        ...

    def read_rows(self, url: str) -> List[List[str]]:
        ...

    def read_cell(self, url: str, row_index: int, column_name: str) -> str:
        ...

    def update_cell(self, url: str, row_index: int, updates: Dict[str, Any]) -> bool:
        ...

    def update_row(self, url: str, row_index: int, values: Dict[str, Any]) -> bool:
        ...

    def save(self) -> bool:
        ...

    def close(self) -> None:
        ...


class PlaywrightSpreadsheetDriver:
    """
    Playwright-backed spreadsheet driver.
    Interacts with HTML-rendered spreadsheets (Google Sheets, Excel Online, HTML tables)
    without bypassing security, logins, or permissions.
    """

    def __init__(self, session: Optional[BrowserSessionInstance] = None):
        self.session = session
        self._current_url: Optional[str] = None
        self._canonical_to_col: Dict[str, int] = {}
        self._col_to_canonical: Dict[int, str] = {}
        self._cached_headers: List[str] = []

    def check_access(self, url: str) -> SourceAccessStatus:
        """
        Navigate to URL and inspect response and DOM to identify accessibility:
        - LOGIN_REQUIRED
        - ACCESS_PROHIBITED
        - UNSUPPORTED_STRUCTURE
        - SOURCE_UNAVAILABLE
        - ACCESSIBLE
        """
        valid_res, err_msg = validate_spreadsheet_url(url)
        if valid_res != UrlValidationResult.VALID_SOURCE:
            return SourceAccessStatus.UNSUPPORTED_STRUCTURE

        if not self.session:
            return SourceAccessStatus.SOURCE_UNAVAILABLE

        try:
            if not self.session.is_alive():
                self.session.start()

            current_page_url = self.session.navigate(url)

            # 1. Detect login wall
            lower_url = current_page_url.lower()
            if any(term in lower_url for term in ("accounts.google.com/signin", "login.microsoftonline.com", "login.live.com", "auth")):
                logger.info("Login required wall encountered for spreadsheet", url=url)
                return SourceAccessStatus.LOGIN_REQUIRED

            # 2. Inspect page text/DOM for access denied or permissions
            content = self.session.evaluate(
                "() => ({ title: document.title, text: document.body ? document.body.innerText.substring(0, 3000) : '' })"
            )
            title = (content.get("title") or "").lower()
            body_sample = (content.get("text") or "").lower()

            if any(term in title or term in body_sample for term in ("sign in", "log in", "choose an account")):
                return SourceAccessStatus.LOGIN_REQUIRED

            if any(term in title or term in body_sample for term in ("access denied", "you need permission", "request access", "403 forbidden")):
                logger.warning("Access prohibited for spreadsheet URL", url=url)
                return SourceAccessStatus.ACCESS_PROHIBITED

            # 3. Verify presence of spreadsheet table elements
            table_check = self.session.evaluate(
                """() => {
                    const hasTable = document.querySelector('table') !== null;
                    const hasGrid = document.querySelector('[role="grid"]') !== null;
                    const hasWaffle = document.querySelector('#waffle-grid-container') !== null;
                    const hasOffice = document.querySelector('.ewa-grid') !== null;
                    return hasTable || hasGrid || hasWaffle || hasOffice;
                }"""
            )

            if not table_check:
                # Page loaded, but does not present a recognizable spreadsheet or table structure
                logger.warning("Page loaded but no recognizable spreadsheet structure found", url=url)
                return SourceAccessStatus.UNSUPPORTED_STRUCTURE

            return SourceAccessStatus.ACCESSIBLE

        except BrowserTimeoutError:
            return SourceAccessStatus.SOURCE_UNAVAILABLE
        except BrowserNavigationError:
            return SourceAccessStatus.SOURCE_UNAVAILABLE
        except Exception as e:
            logger.error(f"Error checking spreadsheet access: {e}", url=url)
            return SourceAccessStatus.SOURCE_UNAVAILABLE

    def open(self, url: str) -> bool:
        """Open and verify access to spreadsheet URL."""
        status = self.check_access(url)
        if status != SourceAccessStatus.ACCESSIBLE:
            return False
        self._current_url = url
        return True

    def read_headers(self, url: str) -> List[str]:
        """Read column header labels from spreadsheet."""
        if not self.session or not self.session.is_alive():
            raise SourceAccessError("Browser session not available", code=ErrorCode.SOURCE_UNAVAILABLE)

        # Extract headers from table thead, first tr, or role='columnheader'
        headers = self.session.evaluate(
            """() => {
                // Priority 1: role="columnheader"
                const colHeaders = document.querySelectorAll('[role="columnheader"]');
                if (colHeaders && colHeaders.length > 0) {
                    return Array.from(colHeaders).map(el => (el.innerText || '').trim());
                }

                // Priority 2: first tr in thead or table
                const firstRow = document.querySelector('table tr');
                if (firstRow) {
                    const cells = firstRow.querySelectorAll('th, td');
                    if (cells && cells.length > 0) {
                        return Array.from(cells).map(c => (c.innerText || '').trim());
                    }
                }

                return [];
            }"""
        )

        if not headers:
            raise ValidationError("Could not detect any headers in the browser spreadsheet")

        self._cached_headers = [str(h).strip() for h in headers]
        canonical_map, col_map = SpreadsheetStructureValidator.validate_headers(self._cached_headers)
        self._canonical_to_col = canonical_map
        self._col_to_canonical = col_map
        return self._cached_headers

    def read_rows(self, url: str) -> List[List[str]]:
        """Read data rows from HTML spreadsheet."""
        if not self.session or not self.session.is_alive():
            raise SourceAccessError("Browser session not available", code=ErrorCode.SOURCE_UNAVAILABLE)

        raw_rows = self.session.evaluate(
            """() => {
                const trs = Array.from(document.querySelectorAll('table tr'));
                if (trs.length <= 1) {
                    // Try role="row"
                    const roleRows = Array.from(document.querySelectorAll('[role="row"]'));
                    if (roleRows.length > 1) {
                        return roleRows.slice(1).map(r => {
                            const cells = r.querySelectorAll('[role="gridcell"]');
                            return Array.from(cells).map(c => (c.innerText || '').trim());
                        });
                    }
                    return [];
                }

                // Slice off header row
                return trs.slice(1).map(tr => {
                    const cells = tr.querySelectorAll('td');
                    return Array.from(cells).map(c => (c.innerText || '').trim());
                });
            }"""
        )
        return raw_rows or []

    def read_sheet(self, url: str) -> List[SourceRow]:
        """Read spreadsheet headers and rows, returning canonical SourceRow items."""
        if not self._cached_headers:
            self.read_headers(url)

        raw_rows = self.read_rows(url)
        source_rows: List[SourceRow] = []

        for idx, row_cells in enumerate(raw_rows):
            row_index = idx + 2  # 1-based, header is row 1
            parsed = SpreadsheetStructureValidator.parse_row(
                row_cells=row_cells,
                row_index=row_index,
                idx_to_canonical=self._col_to_canonical,
            )
            if parsed:
                source_rows.append(parsed)

        return source_rows

    def read_cell(self, url: str, row_index: int, column_name: str) -> str:
        """Read value of a specific cell by row and column name."""
        if not self._cached_headers:
            self.read_headers(url)

        col_idx = self._canonical_to_col.get(column_name)
        if col_idx is None:
            raise ValidationError(f"Column '{column_name}' not found in spreadsheet")

        # row_index is 1-based data index (row 2 is first data row)
        data_tr_idx = row_index - 2
        value = self.session.evaluate(
            """([trIdx, colIdx]) => {
                const trs = document.querySelectorAll('table tr');
                // plus 1 to skip header row
                const tr = trs[trIdx + 1];
                if (!tr) return '';
                const cells = tr.querySelectorAll('td');
                const cell = cells[colIdx];
                return cell ? (cell.innerText || '').trim() : '';
            }""",
            [data_tr_idx, col_idx],
        )
        return str(value or "").strip()

    def update_cell(self, url: str, row_index: int, updates: Dict[str, Any]) -> bool:
        """
        Explicit safe write-back:
        1. Validate target row and column.
        2. Read current value.
        3. Write new value.
        4. Read back and verify update.
        """
        if not self.session or not self.session.is_alive():
            raise SourceAccessError("Browser session not available for update", code=ErrorCode.SOURCE_UNAVAILABLE)

        if not self._cached_headers:
            self.read_headers(url)

        data_tr_idx = row_index - 2

        for field_name, new_val in updates.items():
            col_idx = self._canonical_to_col.get(field_name)
            if col_idx is None:
                raise ValidationError(f"Cannot update unknown column '{field_name}'")

            target_val_str = str(new_val)

            # Perform write via JavaScript in DOM
            written = self.session.evaluate(
                """([trIdx, colIdx, newVal]) => {
                    const trs = document.querySelectorAll('table tr');
                    const tr = trs[trIdx + 1];
                    if (!tr) return false;
                    const cells = tr.querySelectorAll('td');
                    const cell = cells[colIdx];
                    if (!cell) return false;
                    cell.innerText = newVal;
                    cell.dispatchEvent(new Event('input', { bubbles: true }));
                    cell.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                }""",
                [data_tr_idx, col_idx, target_val_str],
            )

            if not written:
                raise SourceAccessError(f"Target cell at row {row_index}, col {col_idx} could not be updated", code=ErrorCode.SYNC_CONFLICT)

            # Step 5: Read back and verify
            readback = self.read_cell(url, row_index, field_name)
            if readback != target_val_str:
                raise ConflictError(
                    f"Write-back verification failed for row {row_index} col '{field_name}'. Expected '{target_val_str}', got '{readback}'"
                )

        return True

    def update_row(self, url: str, row_index: int, values: Dict[str, Any]) -> bool:
        return self.update_cell(url, row_index, values)

    def save(self) -> bool:
        return True

    def close(self) -> None:
        self._current_url = None
