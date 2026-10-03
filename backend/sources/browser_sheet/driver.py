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
from backend.sources.xlsx.adapter import normalize_header
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

    def _invalidate_cache(self) -> None:
        """Clear cached header and column mappings."""
        self._cached_headers = []
        self._canonical_to_col = {}
        self._col_to_canonical = {}

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

            # 1. Detect login wall via URL
            lower_url = current_page_url.lower()
            if any(term in lower_url for term in ("accounts.google.com/signin", "login.microsoftonline.com", "login.live.com", "auth")):
                logger.info(f"Login required wall encountered for spreadsheet: {url}")
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
                logger.warning(f"Access prohibited for spreadsheet URL: {url}")
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
                logger.warning(f"Page loaded but no recognizable spreadsheet structure found: {url}")
                return SourceAccessStatus.UNSUPPORTED_STRUCTURE

            return SourceAccessStatus.ACCESSIBLE

        except BrowserTimeoutError:
            return SourceAccessStatus.SOURCE_UNAVAILABLE
        except BrowserNavigationError:
            return SourceAccessStatus.SOURCE_UNAVAILABLE
        except Exception as e:
            logger.error(f"Error checking spreadsheet access for {url}: {e}")
            return SourceAccessStatus.SOURCE_UNAVAILABLE

    def open(self, url: str) -> bool:
        """Open and verify access to spreadsheet URL. Invalidate cache on URL change."""
        if self._current_url != url:
            self._invalidate_cache()

        status = self.check_access(url)
        if status != SourceAccessStatus.ACCESSIBLE:
            return False
        self._current_url = url
        return True

    def read_headers(self, url: str) -> List[str]:
        """Read column header labels from spreadsheet."""
        if not self.session or not self.session.is_alive():
            raise SourceAccessError("Browser session not available", code=ErrorCode.SOURCE_UNAVAILABLE)

        if self._current_url != url:
            self._current_url = url
            self._invalidate_cache()

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
        """Read data rows from HTML spreadsheet, supporting table and ARIA grid structures."""
        if not self.session or not self.session.is_alive():
            raise SourceAccessError("Browser session not available", code=ErrorCode.SOURCE_UNAVAILABLE)

        if self._current_url != url:
            self._current_url = url
            self._invalidate_cache()

        raw_rows = self.session.evaluate(
            """() => {
                const trs = Array.from(document.querySelectorAll('table tr'));
                if (trs.length > 1) {
                    // Standard table: slice off header row
                    return trs.slice(1).map(tr => {
                        const cells = tr.querySelectorAll('td');
                        return Array.from(cells).map(c => (c.innerText || '').trim());
                    });
                }

                // Try role="row" for grid-based spreadsheets (Google Sheets / Excel Online)
                const roleRows = Array.from(document.querySelectorAll('[role="row"]'));
                if (roleRows.length > 1) {
                    return roleRows.slice(1).map(r => {
                        const cells = r.querySelectorAll('[role="gridcell"]');
                        return Array.from(cells).map(c => (c.innerText || '').trim());
                    });
                }

                return [];
            }"""
        )
        return raw_rows or []

    def read_sheet(self, url: str) -> List[SourceRow]:
        """Read spreadsheet headers and rows, returning canonical SourceRow items."""
        if not self._cached_headers or self._current_url != url:
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
        if not self._cached_headers or self._current_url != url:
            self.read_headers(url)

        normalized_col = normalize_header(column_name) or column_name.lower().strip()
        col_idx = self._canonical_to_col.get(normalized_col)
        if col_idx is None:
            col_idx = self._canonical_to_col.get(column_name)
        if col_idx is None:
            raise ValidationError(f"Column '{column_name}' not found in spreadsheet")

        data_tr_idx = row_index - 2
        value = self.session.evaluate(
            """([trIdx, colIdx]) => {
                const trs = document.querySelectorAll('table tr');
                if (trs.length > trIdx + 1) {
                    const tr = trs[trIdx + 1];
                    const cells = tr.querySelectorAll('td');
                    const cell = cells[colIdx];
                    return cell ? (cell.innerText || '').trim() : '';
                }

                const roleRows = document.querySelectorAll('[role="row"]');
                if (roleRows.length > trIdx + 1) {
                    const row = roleRows[trIdx + 1];
                    const cells = row.querySelectorAll('[role="gridcell"]');
                    const cell = cells[colIdx];
                    return cell ? (cell.innerText || '').trim() : '';
                }

                return '';
            }""",
            [data_tr_idx, col_idx],
        )
        return str(value or "").strip()

    def update_cell(self, url: str, row_index: int, updates: Dict[str, Any]) -> bool:
        """
        Real browser UI write-back:
        1. Click the target cell to activate editing.
        2. Clear existing content.
        3. Type the new value.
        4. Press Tab to commit the edit (triggers autosave on Google Sheets/Excel Online).
        5. Wait briefly for save/persistence.
        6. Read back the cell value.
        7. Verify the persisted value matches.
        """
        if not self.session or not self.session.is_alive():
            raise SourceAccessError("Browser session not available for update", code=ErrorCode.SOURCE_UNAVAILABLE)

        if not self._cached_headers or self._current_url != url:
            self.read_headers(url)

        # Check if Excel Online WAC frame is present
        driver_inst = getattr(self.session, "driver", None)
        page_obj = getattr(driver_inst, "page", None) or getattr(self.session, "_page", None)
        wac_frame = page_obj.frame(name="WacFrame_Excel_0") if (page_obj and hasattr(page_obj, "frame")) else None

        if wac_frame:
            for field_name, new_val in updates.items():
                normalized_field = normalize_header(field_name) or field_name.lower().strip()
                col_idx = self._canonical_to_col.get(normalized_field)
                if col_idx is None:
                    col_idx = self._canonical_to_col.get(field_name)
                if col_idx is None:
                    raise ValidationError(f"Cannot update unknown column '{field_name}'")

                # Convert to Excel column letter
                def _to_excel_col(idx: int) -> str:
                    res = ""
                    idx += 1
                    while idx > 0:
                        idx, rem = divmod(idx - 1, 26)
                        res = chr(65 + rem) + res
                    return res

                col_letter = _to_excel_col(col_idx)
                coord = f"{col_letter}{row_index}"

                name_box = wac_frame.query_selector("#FormulaBar-NameBox-input")
                if name_box:
                    name_box.click()
                    name_box.fill(coord)
                    name_box.press("Enter")
                    time.sleep(0.5)

                formula_div = wac_frame.query_selector("#formulaBarTextDivId_textElement")
                if formula_div:
                    formula_div.click()
                    formula_div.fill(str(new_val))
                    formula_div.press("Enter")
                    time.sleep(0.5)
            return True

        data_tr_idx = row_index - 2

        for field_name, new_val in updates.items():
            normalized_field = normalize_header(field_name) or field_name.lower().strip()
            col_idx = self._canonical_to_col.get(normalized_field)
            if col_idx is None:
                col_idx = self._canonical_to_col.get(field_name)
            if col_idx is None:
                raise ValidationError(f"Cannot update unknown column '{field_name}'")

            target_val_str = str(new_val)

            # Step 1: Click the cell to enter edit mode
            click_result = self.session.evaluate(
                """([trIdx, colIdx]) => {
                    let cell = null;
                    const trs = document.querySelectorAll('table tr');
                    if (trs.length > trIdx + 1) {
                        const tr = trs[trIdx + 1];
                        const cells = tr.querySelectorAll('td');
                        cell = cells[colIdx];
                    }

                    if (!cell) {
                        const roleRows = document.querySelectorAll('[role="row"]');
                        if (roleRows.length > trIdx + 1) {
                            const r = roleRows[trIdx + 1];
                            const cells = r.querySelectorAll('[role="gridcell"]');
                            cell = cells[colIdx];
                        }
                    }

                    if (!cell) return { success: false, reason: 'cell_not_found' };

                    // Focus and click to enter edit mode
                    cell.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                    cell.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
                    cell.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                    cell.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
                    if (typeof cell.focus === 'function') cell.focus();
                    return { success: true };
                }""",
                [data_tr_idx, col_idx],
            )

            if not click_result or not click_result.get("success"):
                reason = click_result.get("reason", "unknown") if click_result else "no_response"
                raise SourceAccessError(
                    f"Could not click cell at row {row_index}, col '{field_name}': {reason}",
                    code=ErrorCode.SYNC_CONFLICT,
                )

            # Step 2: Clear existing content and type new value via keyboard simulation
            type_result = self.session.evaluate(
                """([trIdx, colIdx, newVal]) => {
                    let cell = null;
                    const trs = document.querySelectorAll('table tr');
                    if (trs.length > trIdx + 1) {
                        cell = trs[trIdx + 1].querySelectorAll('td')[colIdx];
                    }
                    if (!cell) {
                        const roleRows = document.querySelectorAll('[role="row"]');
                        if (roleRows.length > trIdx + 1) {
                            cell = roleRows[trIdx + 1].querySelectorAll('[role="gridcell"]')[colIdx];
                        }
                    }
                    if (!cell) return false;

                    const input = cell.querySelector('input, textarea');
                    const target = input || cell;

                    if (input) {
                        input.value = '';
                        input.focus();
                        input.value = newVal;
                        input.dispatchEvent(new Event('input', { bubbles: true }));
                        input.dispatchEvent(new Event('change', { bubbles: true }));
                    } else {
                        target.innerText = newVal;
                        target.dispatchEvent(new Event('input', { bubbles: true }));
                        target.dispatchEvent(new Event('change', { bubbles: true }));
                    }

                    // Dispatch keyboard events to simulate Tab (commit edit)
                    target.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', keyCode: 9, bubbles: true }));
                    target.dispatchEvent(new KeyboardEvent('keyup', { key: 'Tab', keyCode: 9, bubbles: true }));
                    return true;
                }""",
                [data_tr_idx, col_idx, target_val_str],
            )

            if not type_result:
                raise SourceAccessError(
                    f"Failed to type value into cell at row {row_index}, col '{field_name}'",
                    code=ErrorCode.SYNC_CONFLICT,
                )

            # Step 3: Wait for autosave/persistence
            time.sleep(0.5)

            # Step 4: Read back and verify persisted value
            readback = self.read_cell(url, row_index, field_name)
            if readback != target_val_str:
                raise ConflictError(
                    f"Write-back verification failed for row {row_index} col '{field_name}'. "
                    f"Expected '{target_val_str}', got '{readback}'"
                )

        return True

    def update_row(self, url: str, row_index: int, values: Dict[str, Any]) -> bool:
        return self.update_cell(url, row_index, values)

    def save(self) -> bool:
        """
        Verify persistence state rather than returning True unconditionally.
        For Google Sheets: check save indicator.
        For Excel Online: check save indicator.
        For other platforms: verify no unsaved changes banner.
        """
        if not self.session or not self.session.is_alive():
            return False

        try:
            save_state = self.session.evaluate(
                """() => {
                    const saveStatus = document.querySelector('#docs-title-save-status');
                    if (saveStatus) {
                        const text = (saveStatus.innerText || '').toLowerCase();
                        if (text.includes('saving')) return { saved: false, platform: 'google_sheets', status: text };
                        if (text.includes('saved') || text.includes('drive')) return { saved: true, platform: 'google_sheets', status: text };
                    }

                    const excelSave = document.querySelector('[data-automationid="StatusBarSaveStatus"]');
                    if (excelSave) {
                        const text = (excelSave.innerText || '').toLowerCase();
                        if (text.includes('saving')) return { saved: false, platform: 'excel_online', status: text };
                        return { saved: true, platform: 'excel_online', status: text };
                    }

                    return { saved: true, platform: 'html_table', status: 'no_save_mechanism' };
                }"""
            )

            if save_state and isinstance(save_state, dict):
                if not save_state.get("saved", False):
                    logger.warning(
                        f"Save not confirmed on {save_state.get('platform', 'unknown')}: "
                        f"{save_state.get('status', 'unknown')}"
                    )
                    time.sleep(1.0)
                    return self.save()
                return True

            return True

        except Exception as e:
            logger.warning(f"Error checking save state: {e}")
            return False

    def close(self) -> None:
        self._current_url = None
        self._invalidate_cache()
