"""Deterministic browser spreadsheet page fixture for offline testing.

Simulates a browser-rendered spreadsheet supporting:
- Headers and canonical mapping
- Rows and data extraction
- Editable cells and write-back
- Read-back verification
- State simulation: ACCESSIBLE, LOGIN_REQUIRED, ACCESS_PROHIBITED, SOURCE_UNAVAILABLE, UNSUPPORTED_STRUCTURE
"""

from typing import List, Dict, Any, Optional
from backend.browser.driver import BrowserDriver
from backend.browser.browser_types import BrowserLaunchConfig
from backend.browser.session import BrowserSessionInstance
from backend.browser.exceptions import BrowserCrashError, BrowserSessionError
from backend.domain.enums import SourceAccessStatus, ErrorCode


class DeterministicSpreadsheetPage:
    """In-memory representation of an editable browser-rendered spreadsheet table."""

    DEFAULT_HEADERS = [
        "Name",
        "Instagram URL",
        "Message",
        "Expected Followers",
        "Follow-up 1 Message",
        "Follow-up 1 Delay",
        "Follow-up 2 Message",
        "Follow-up 2 Delay",
        "Replied",
        "Notes",
    ]

    def __init__(
        self,
        headers: Optional[List[str]] = None,
        rows: Optional[List[List[str]]] = None,
        access_state: SourceAccessStatus = SourceAccessStatus.ACCESSIBLE,
    ):
        self.headers = list(headers or self.DEFAULT_HEADERS)
        self.rows: List[List[str]] = list(rows or [
            [
                "Alice Johnson",
                "https://instagram.com/alice_j",
                "Hi Alice, let's connect!",
                "5000",
                "Checking in Alice!",
                "1",
                "Final follow up Alice",
                "3",
                "NO",
                "Lead from event",
            ],
            [
                "Bob Smith",
                "https://instagram.com/bob_smith",
                "Hello Bob!",
                "12000",
                "Hey Bob follow up 1",
                "2",
                "Hey Bob follow up 2",
                "4",
                "YES",
                "Met in person",
            ],
        ])
        self.access_state = access_state
        self.write_back_fails = False

    def get_cell(self, row_idx: int, col_idx: int) -> str:
        """1-based row index, 0-based col index."""
        data_row = row_idx - 2  # row 1 is header, row 2 is index 0
        if 0 <= data_row < len(self.rows):
            row = self.rows[data_row]
            if 0 <= col_idx < len(row):
                return row[col_idx]
        return ""

    def set_cell(self, row_idx: int, col_idx: int, value: str) -> bool:
        """1-based row index, 0-based col index."""
        if self.write_back_fails:
            return False
        data_row = row_idx - 2
        if 0 <= data_row < len(self.rows):
            row = self.rows[data_row]
            while len(row) <= col_idx:
                row.append("")
            row[col_idx] = value
            return True
        return False


class DeterministicSpreadsheetDriver(BrowserDriver):
    """
    Mock BrowserDriver that executes Javascript evaluate queries against
    a DeterministicSpreadsheetPage instance.
    """

    def __init__(self, page: Optional[DeterministicSpreadsheetPage] = None):
        self.page = page or DeterministicSpreadsheetPage()
        self._connected = True
        self._current_url = "https://docs.google.com/spreadsheets/d/test_sheet/edit"
        self.navigate_history: List[str] = []

    def launch(self, config: Optional[BrowserLaunchConfig] = None) -> None:
        self._connected = True

    def close(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def new_context(self, profile_path: Optional[str] = None) -> None:
        pass

    def close_context(self) -> None:
        pass

    def new_page(self) -> None:
        pass

    def close_page(self) -> None:
        pass

    def navigate(self, url: str, timeout_ms: Optional[int] = None) -> str:
        if not self._connected:
            raise BrowserCrashError("Browser disconnected")
        self._current_url = url
        self.navigate_history.append(url)
        return url

    def current_url(self) -> str:
        return self._current_url

    def wait_for_load(self, state: str = "load", timeout_ms: Optional[int] = None) -> None:
        pass

    def evaluate(self, expression: str, arg: Any = None) -> Any:
        if not self._connected:
            raise BrowserCrashError("Browser disconnected")

        # 1. State simulation for check_access()
        if self.page.access_state == SourceAccessStatus.SOURCE_UNAVAILABLE:
            raise BrowserCrashError("Source unavailable")

        if "title: document.title" in expression:
            if self.page.access_state == SourceAccessStatus.LOGIN_REQUIRED:
                return {"title": "Google Accounts - Sign in", "text": "Sign in to continue"}
            elif self.page.access_state == SourceAccessStatus.ACCESS_PROHIBITED:
                return {"title": "Access Denied", "text": "You need permission to access this document"}
            return {"title": "Test Spreadsheet - Google Sheets", "text": "Spreadsheet content"}

        if "hasTable" in expression:
            if self.page.access_state == SourceAccessStatus.UNSUPPORTED_STRUCTURE:
                return False
            return True

        # 2. Header extraction
        if "role=\"columnheader\"" in expression or "firstRow = document.querySelector('table tr')" in expression:
            if self.page.access_state == SourceAccessStatus.UNSUPPORTED_STRUCTURE:
                return []
            return list(self.page.headers)

        # 3. Row extraction
        if "document.querySelectorAll('table tr')" in expression and "slice(1)" in expression:
            return [list(r) for r in self.page.rows]

        # 4. Cell click/focus simulation
        if "click_result" in expression or "MouseEvent" in expression:
            return {"success": True}

        # 5. Cell update (write-back)
        if "newVal" in expression or "KeyboardEvent" in expression:
            # arg is [trIdx, col_index, new_value]
            if isinstance(arg, (list, tuple)) and len(arg) >= 3:
                data_tr_idx, col_idx, new_val = arg[0], arg[1], str(arg[2])
                success = self.page.set_cell(data_tr_idx + 2, col_idx, new_val)
                return success
            return True

        # 6. Read back specific cell value
        if "document.querySelectorAll('table tr')" in expression and ("cells[colIdx]" in expression or "tds[colIdx]" in expression):
            if isinstance(arg, (list, tuple)) and len(arg) >= 2:
                data_tr_idx, col_idx = arg[0], arg[1]
                return self.page.get_cell(data_tr_idx + 2, col_idx)
            return ""

        # Default
        return True

    def screenshot(self, path: str) -> None:
        pass

    def get_content(self) -> str:
        return "<html><body><table></table></body></html>"


def create_deterministic_session(
    page: Optional[DeterministicSpreadsheetPage] = None,
    session_id: str = "test-det-sess",
    worker_id: Optional[str] = "test-det-wkr",
) -> BrowserSessionInstance:
    """Create a BrowserSessionInstance backed by a DeterministicSpreadsheetDriver."""
    driver = DeterministicSpreadsheetDriver(page=page)
    session = BrowserSessionInstance(
        session_id=session_id,
        driver=driver,
        worker_id=worker_id,
    )
    session.start()
    return session
